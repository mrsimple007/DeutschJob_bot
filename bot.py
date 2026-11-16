import os, re, json, base64, asyncio, logging, smtplib, ssl, time, threading, html
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path

import dotenv
import httpx
import docx
import google.generativeai as genai
from google.api_core import exceptions as gexc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, PicklePersistence, filters,
)

dotenv.load_dotenv()

MAX_SHOW = int(os.getenv("MAX_SHOW", "10"))
LEVELS = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "NATIVE": 7}
GER_REQ_RE = re.compile(r"\b(C1|C2)\b|verhandlungssicher|flie(ß|ss)end|muttersprach", re.I)


# ------------------------------------------------------------------ logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs full URLs at INFO, which includes the Telegram bot token. Silence it.
for noisy in ("httpx", "httpcore", "telegram", "hpack"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("jobbot")


@contextmanager
def step(name: str):
    """Log start/end/duration of a processing step."""
    t0 = time.time()
    log.info("▶ %s ...", name)
    try:
        yield
    except Exception:
        log.error("✖ %s FAILED after %.1fs", name, time.time() - t0)
        raise
    else:
        log.info("✔ %s done in %.1fs", name, time.time() - t0)


# ------------------------------------------------------------------ config
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

BA_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
SEARCH_PATHS = ["/pc/v6/jobs", "/pc/v4/app/jobs", "/pc/v4/jobs"]
DETAIL_PATHS = ["/pc/v4/jobdetails/{}", "/pc/v3/jobdetails/{}"]

CV_DIR = Path("cvs")
CV_DIR.mkdir(exist_ok=True)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


# ------------------------------------------------------------------ Gemini key pool
def load_keys() -> list:
    names = ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(1, 21)]
    keys = []
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v and v not in keys:
            keys.append(v)
    if not keys:
        raise RuntimeError("No Gemini API key found. Set GEMINI_API_KEY (and optionally GEMINI_API_KEY_1, _2, ...)")
    return keys


def quick_info(profile: dict, job: dict):
    """Free local check, no LLM."""
    text = f"{job['title']} {job['description']}".lower()
    skills = [s for s in (profile.get("skills") or []) if s and s.lower() in text]
    words = {w.lower() for t in (profile.get("job_titles") or [])
             for w in re.split(r"\W+", t) if len(w) >= 5}
    title_hits = sum(1 for w in words if w in text)
    job["matched_skills"] = skills[:6]
    job["score"] = len(skills) * 2 + title_hits
    my_level = LEVELS.get(str(profile.get("german_level") or "").upper(), 0)
    job["german_warning"] = bool(GER_REQ_RE.search(job["description"])) and my_level < 5


LETTER_PROMPT = """Write a German cover letter (Anschreiben) for this job application.
Return ONLY JSON: {{"subject": str, "body": str}}.
Rules:
- body starts with "Sehr geehrte Damen und Herren," and ends with "Mit freundlichen Grüßen" and the candidate's full name on the next line.
- 170-230 words, formal, concrete. Connect 2-3 REAL skills or experiences from the profile to the job requirements.
- Use ONLY facts from the candidate profile. Never invent employers, degrees, numbers, certificates,
  language levels, a start date, or a move to Germany. Do not mention missing requirements.
- subject: "Bewerbung als <job title> (Referenznummer <refnr>)".

CANDIDATE PROFILE:
{profile}

JOB:
title: {title}
employer: {employer}
refnr: {refnr}
description: {description}
"""


def write_letter_sync(profile: dict, job: dict) -> dict:
    prompt = LETTER_PROMPT.format(
        profile=json.dumps(profile, ensure_ascii=False),
        title=job["title"], employer=job["employer"], refnr=job["refnr"],
        description=(job["description"] or "")[:3000],
    )
    data = gemini_json(prompt, label="cover-letter")
    if isinstance(data, list) and data:
        data = data[0]
    return {"subject": data["subject"], "body": data["body"]}


def letter_docx(uid: int, job: dict, letter: dict) -> Path:
    d = docx.Document()
    d.add_paragraph(letter["subject"]).runs[0].bold = True
    for para in letter["body"].split("\n\n"):
        d.add_paragraph(para)
    
    # Backslash (\) o'z ichiga olgan regex amalini f-string'dan tashqarida bajaramiz
    clean_refnr = re.sub(r'[^\w-]', '_', job['refnr'])
    path = CV_DIR / f"letter_{uid}_{clean_refnr}.docx"
    
    d.save(str(path))
    return path


class KeyPool:
    def __init__(self, keys):
        self.keys = keys
        self.idx = 0
        self.cooldown = {}   # key index -> unix time when usable again
        self.dead = set()    # permanently invalid keys
        self.lock = threading.Lock()

    def acquire(self) -> int:
        """Return index of a usable key; sleeps if all are cooling down."""
        while True:
            with self.lock:
                n = len(self.keys)
                alive = [i for i in range(n) if i not in self.dead]
                if not alive:
                    raise RuntimeError("All Gemini API keys are invalid or unauthorized.")
                now = time.time()
                for off in range(n):
                    i = (self.idx + off) % n
                    if i not in self.dead and self.cooldown.get(i, 0) <= now:
                        self.idx = i
                        return i
                wait = min(self.cooldown.get(i, 0) for i in alive) - now
            wait = min(max(wait, 1), 65)
            log.warning("[gemini] all keys rate-limited, waiting %.0fs", wait)
            time.sleep(wait)

    def cool(self, i: int, seconds: int):
        with self.lock:
            self.cooldown[i] = time.time() + seconds
            self.idx = (i + 1) % len(self.keys)

    def kill(self, i: int):
        with self.lock:
            self.dead.add(i)
            self.idx = (i + 1) % len(self.keys)


POOL = KeyPool(load_keys())
_genai_lock = threading.Lock()  # genai.configure() is global, so serialize calls
log.info("Loaded %d Gemini API key(s), model=%s, DRY_RUN=%s", len(POOL.keys), MODEL, DRY_RUN)


def gemini_json(parts, label: str = "gemini"):
    """Call Gemini expecting JSON. Rotates keys on quota/auth errors, backs off on transient ones."""
    max_attempts = max(8, len(POOL.keys) * 3)
    last_err = None
    for attempt in range(1, max_attempts + 1):
        ki = POOL.acquire()
        tag = f"{label} key#{ki} attempt {attempt}/{max_attempts}"
        try:
            log.info("[gemini] %s: request", tag)
            with _genai_lock:
                genai.configure(api_key=POOL.keys[ki])
                model = genai.GenerativeModel(
                    MODEL,
                    generation_config={"response_mime_type": "application/json", "temperature": 0.2},
                )
                resp = model.generate_content(parts, request_options={"timeout": 120})
                text = resp.text
            data = json.loads(text)
            log.info("[gemini] %s: OK", tag)
            return data

        except gexc.ResourceExhausted as e:
            last_err = e
            log.warning("[gemini] %s: quota/rate limit -> switching key (cooldown 60s)", tag)
            POOL.cool(ki, 60)
        except (gexc.PermissionDenied, gexc.Unauthenticated) as e:
            last_err = e
            log.error("[gemini] %s: key rejected (%s) -> disabling this key", tag, type(e).__name__)
            POOL.kill(ki)
        except gexc.InvalidArgument as e:
            last_err = e
            if "api key" in str(e).lower():
                log.error("[gemini] %s: invalid API key -> disabling it", tag)
                POOL.kill(ki)
            else:
                log.error("[gemini] %s: bad request: %s", tag, e)
                raise
        except (gexc.ServiceUnavailable, gexc.InternalServerError, gexc.DeadlineExceeded,
                gexc.TooManyRequests) as e:
            last_err = e
            delay = min(2 ** attempt, 20)
            log.warning("[gemini] %s: transient %s -> retry in %ds", tag, type(e).__name__, delay)
            time.sleep(delay)
        except (json.JSONDecodeError, ValueError) as e:
            # invalid JSON, or response blocked/empty (resp.text raises ValueError)
            last_err = e
            log.warning("[gemini] %s: bad/empty response (%s) -> retry", tag, e)
            time.sleep(1)
    raise RuntimeError(f"Gemini failed after {max_attempts} attempts: {last_err}")


PROFILE_PROMPT = """You are parsing a CV. Return ONLY JSON with these keys:
full_name, email, phone, location (current city), seniority (junior/mid/senior/lead),
years_experience (number),
job_titles (4-6 SHORT job titles, 1-2 words each, in GERMAN exactly as they appear in
German job ads, e.g. "Disponent", "Logistikmitarbeiter", "Werkstudent Logistik",
"Sachbearbeiter Logistik"; avoid English titles and long phrases; most relevant first),
skills (list), german_level (A1-C2, native, or unknown), english_level,
education (short string), summary (2 sentences).
Use null if something is not in the CV. Do not invent information."""

def parse_cv_sync(path: Path) -> dict:
    if path.suffix.lower() == ".pdf":
        part = {"mime_type": "application/pdf", "data": path.read_bytes()}
        return gemini_json([PROFILE_PROMPT, part], label="parse-cv")
    d = docx.Document(str(path))
    text = "\n".join(p.text for p in d.paragraphs)
    for t in d.tables:
        for row in t.rows:
            text += "\n" + " | ".join(c.text for c in row.cells)
    return gemini_json([PROFILE_PROMPT, text], label="parse-cv")

# ------------------------------------------------------------------ Bundesagentur API
_search_path_cache = None


def _ordered(paths, cached):
    return ([cached] if cached else []) + [p for p in paths if p != cached]


async def ba_search(client: httpx.AsyncClient, what: str, where, size: int = 10) -> list:
    """Search jobs. Tries several endpoint versions; a 403/404 means that route doesn't exist."""
    global _search_path_cache
    params = {"was": what, "size": size, "page": 1, "angebotsart": 1}
    if where:
        params["wo"] = where
        params["umkreis"] = 50
    last = "no attempt"
    for path in _ordered(SEARCH_PATHS, _search_path_cache):
        for attempt in range(1, 4):
            try:
                r = await client.get(BA_BASE + path, params=params, headers=BA_HEADERS)
            except httpx.TransportError as e:
                last = f"{path}: network error {e!r}"
                log.warning("[ba] %s attempt %d: %s", path, attempt, last)
                await asyncio.sleep(attempt * 2)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{path}: HTTP {r.status_code}"
                log.warning("[ba] %s attempt %d: HTTP %d -> retry", path, attempt, r.status_code)
                await asyncio.sleep(attempt * 2)
                continue
            if r.status_code in (400, 403, 404):
                last = f"{path}: HTTP {r.status_code} {r.text[:100]!r}"
                log.warning("[ba] %s -> HTTP %d (%s), trying next endpoint", path, r.status_code, r.text[:100])
                break
            r.raise_for_status()
            data = r.json()
            if _search_path_cache != path:
                log.info("[ba] working search endpoint: %s", path)
                _search_path_cache = path
            hits = extract_hits(data)
            log.info("[ba] '%s' (where=%s) -> %d hits via %s", what, where or "Germany", len(hits), path)
            if hits and attempt == 1:
                log.debug("[ba] first hit keys: %s", list(hits[0].keys()))
            return hits
    raise RuntimeError(f"Bundesagentur search failed for '{what}': {last}")


def extract_hits(data) -> list:
    if isinstance(data, list):
        return data
    for key in ("ergebnisliste", "stellenangebote", "jobs", "items", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    if data.get("maxErgebnisse", 0) == 0 or "ergebnisliste" not in data:
        return []  # genuinely empty result
    log.warning("[ba] unknown response shape, top-level keys: %s", list(data.keys()))
    return []

async def ba_details(client: httpx.AsyncClient, refnr: str) -> dict:
    enc = base64.b64encode(refnr.encode()).decode()
    for tmpl in DETAIL_PATHS:
        path = tmpl.format(enc)
        try:
            r = await client.get(BA_BASE + path, headers=BA_HEADERS)
            if r.status_code == 200:
                return r.json()
            log.warning("[ba] details %s -> HTTP %d", refnr, r.status_code)
        except Exception as e:
            log.warning("[ba] details %s failed: %r", refnr, e)
    return {}


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default

def clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


async def search_with_fallback(client, title: str, where):
    hits = await ba_search(client, title, where)
    if hits:
        return hits
    for w in re.split(r"[\s/,\-]+", title):
        if len(w) >= 5 and w.lower() != title.lower():
            log.info("[ba] no hits for '%s', retrying with keyword '%s'", title, w)
            hits = await ba_search(client, w, where)
            if hits:
                return hits
    return []

async def find_jobs(titles: list, where, limit: int = 15) -> list:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        with step(f"BA search for {len(titles[:3])} titles (where={where or 'Germany'})"):
            results = await asyncio.gather(
                *[search_with_fallback(client, t, where) for t in titles[:4]], return_exceptions=True
            )
        hits, errors = [], []
        for t, res in zip(titles[:4], results):
            if isinstance(res, Exception):
                log.error("[ba] search '%s' failed: %s", t, res)
                errors.append(str(res))
            else:
                hits.extend(res)
        if not hits and errors:
            raise RuntimeError(errors[0])

        seen, base = set(), []
        for h in hits:
            ref = _first(h, "refnr", "referenznummer", "hashId")
            if ref and ref not in seen:
                seen.add(ref)
                h["_ref"] = ref
                base.append(h)
        base = base[:limit]
        log.info("[jobs] %d unique jobs, fetching details", len(base))
        with step(f"fetch details for {len(base)} jobs"):
            details = await asyncio.gather(*[ba_details(client, h["_ref"]) for h in base])

    jobs = []
    for h, d in zip(base, details):
        blob = json.dumps(d, ensure_ascii=False)
        emails = [e for e in EMAIL_RE.findall(blob)
                  if not e.lower().endswith((".png", ".jpg", ".gif", ".webp"))]
        # v6: stellenlokationen[0].adresse.ort   (v4: arbeitsort.ort)
        place = ""
        locs = h.get("stellenlokationen")
        if isinstance(locs, list) and locs:
            place = (locs[0].get("adresse") or {}).get("ort", "")
        elif isinstance(h.get("arbeitsort"), dict):
            place = h["arbeitsort"].get("ort", "")
        ref = h["_ref"]
        jobs.append({
            "refnr": ref,
            "title": _first(h, "stellenangebotsTitel", "titel", "beruf", default="?"),
            "employer": _first(h, "firma", "arbeitgeber", "arbeitgeberName", default="?"),
            "place": place,
            "description": clean_html(_first(d, "stellenangebotsBeschreibung", "stellenbeschreibung", default="")),
            "email": emails[0] if emails else None,
            "url": h.get("externeUrl") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
        })
    log.info("[jobs] %d jobs ready, %d with email", len(jobs), sum(1 for j in jobs if j["email"]))
    return jobs


# ------------------------------------------------------------------ Email
def send_application_sync(profile: dict, job: dict, cv_path: Path, letter: dict):
    to_addr = SMTP_USER if DRY_RUN else job["email"]
    log.info("[mail] sending application for %s to %s (dry_run=%s)", job["refnr"], to_addr, DRY_RUN)
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = to_addr
    if profile.get("email"):
        msg["Reply-To"] = profile["email"]
    msg["Subject"] = ("[TEST] " if DRY_RUN else "") + letter["subject"]
    msg.set_content(letter["body"])
    subtype = "pdf" if cv_path.suffix.lower() == ".pdf" else \
        "vnd.openxmlformats-officedocument.wordprocessingml.document"
    msg.add_attachment(cv_path.read_bytes(), maintype="application", subtype=subtype,
                       filename=f"Lebenslauf_{profile.get('full_name', 'CV')}{cv_path.suffix}")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log.info("[mail] sent OK")
    return to_addr


# ------------------------------------------------------------------ Telegram handlers
def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info("[tg] /start from user %s", update.effective_user.id)
    await update.message.reply_text(
        "Hi! Send me your CV (PDF or DOCX).\n"
        "Then use:\n/search - find and rank jobs\n"
        "/location Berlin - search near a German city (default: all of Germany)\n"
        "/location clear - search all of Germany\n"
        "/profile - show parsed profile"
    )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    ext = Path(doc.file_name or "").suffix.lower()
    log.info("[tg] document from user %s: %s", uid, doc.file_name)
    if ext not in (".pdf", ".docx"):
        await update.message.reply_text("Please send a PDF or DOCX file.")
        return
    msg = await update.message.reply_text("📥 Downloading your CV...")
    path = CV_DIR / f"{uid}{ext}"
    with step("download CV"):
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(path)
    await msg.edit_text("🧠 Reading your CV with AI...")
    try:
        with step("parse CV with Gemini"):
            profile = await asyncio.to_thread(parse_cv_sync, path)
    except Exception as e:
        log.exception("parse failed")
        await msg.edit_text(f"Could not parse the CV: {e}")
        return
    ctx.user_data["profile"] = profile
    ctx.user_data["cv_path"] = str(path)
    ctx.user_data.setdefault("applied", [])
    log.info("[profile] %s | titles=%s", profile.get("full_name"), profile.get("job_titles"))
    await msg.edit_text(format_profile(profile) + "\n\nLooks right? Run /search")


def format_profile(p: dict) -> str:
    return (
        f"👤 {p.get('full_name')}  ({p.get('seniority')}, {p.get('years_experience')} yrs)\n"
        f"📍 Current location: {p.get('location')}   🇩🇪 German: {p.get('german_level')}\n"
        f"🔎 Searching for: {', '.join(p.get('job_titles') or [])}\n"
        f"🛠 {', '.join((p.get('skills') or [])[:12])}"
    )


async def profile_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ctx.user_data.get("profile")
    await update.message.reply_text(format_profile(p) if p else "Send your CV first.")


async def location_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /location Berlin   (or /location clear)")
        return
    val = " ".join(ctx.args)
    if val.lower() in ("clear", "all", "germany", "deutschland"):
        ctx.user_data.pop("location", None)
        await update.message.reply_text("Searching all of Germany.")
    else:
        ctx.user_data["location"] = val
        await update.message.reply_text(f"Search location set to {val}")
    log.info("[tg] user %s location -> %s", update.effective_user.id, ctx.user_data.get("location"))


async def search_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    profile = ctx.user_data.get("profile")
    if not profile:
        await update.message.reply_text("Send your CV first.")
        return
    where = ctx.user_data.get("location")
    titles = profile.get("job_titles") or []
    log.info("[search] user=%s where=%s titles=%s", uid, where or "Germany", titles)
    msg = await update.message.reply_text(f"🔎 Searching jobs in {where or 'all of Germany'}...")
    try:
        jobs = await find_jobs(titles, where)
        jobs = [j for j in jobs if j["refnr"] not in ctx.user_data.get("applied", [])]
    except Exception as e:
        log.exception("search failed")
        await msg.edit_text(f"Search failed: {e}")
        return
    if not jobs:
        await msg.edit_text("No jobs found. Try another city with /location or /location clear.")
        return

    for j in jobs:
        quick_info(profile, j)
    jobs.sort(key=lambda j: j["score"], reverse=True)
    await msg.edit_text(f"📋 Found {len(jobs)} jobs, showing {min(len(jobs), MAX_SHOW)}:")

    ctx.user_data.setdefault("jobs", {})
    for job in jobs[:MAX_SHOW]:
        ctx.user_data["jobs"][job["refnr"]] = job
        lines = [f"<b>{esc(job['title'])}</b>",
                 f"🏢 {esc(job['employer'])} · 📍 {esc(job['place'])}"]
        if job["matched_skills"]:
            lines.append(f"✅ Skills in ad: {esc(', '.join(job['matched_skills']))}")
        if job["german_warning"]:
            lines.append("⚠️ Ad seems to require strong German (C1+)")
        lines.append("✉️ Email: " + esc(job["email"]) if job["email"] else "🔗 Application via website")
        row = [InlineKeyboardButton("✍️ Apply", callback_data=f"apply:{job['refnr']}"),
               InlineKeyboardButton("🔗 Open", url=job["url"]),
               InlineKeyboardButton("Skip", callback_data=f"skip:{job['refnr']}")]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup([row]),
                                        disable_web_page_preview=True)
        await asyncio.sleep(0.4)  # stay under Telegram's flood limits
    log.info("[search] showed %d jobs to user %s", min(len(jobs), MAX_SHOW), uid)


async def make_draft(q, ctx, job):
    uid = q.from_user.id
    note = await q.message.reply_text("✍️ Writing your cover letter...")
    try:
        with step(f"write cover letter {job['refnr']}"):
            letter = await asyncio.to_thread(write_letter_sync, ctx.user_data["profile"], job)
    except Exception as e:
        log.exception("letter failed")
        await note.edit_text(f"Could not write the letter: {e}")
        return
    ctx.user_data.setdefault("drafts", {})[job["refnr"]] = letter
    preview = f"<b>{esc(letter['subject'])}</b>\n\n{esc(letter['body'])}"
    ref = job["refnr"]
    if job["email"]:
        kb = [[InlineKeyboardButton("✉️ Send to employer", callback_data=f"send:{ref}"),
               InlineKeyboardButton("🔄 Rewrite", callback_data=f"regen:{ref}"),
               InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{ref}")]]
        await note.edit_text(preview, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    else:
        kb = [[InlineKeyboardButton("🔗 Open application page", url=job["url"])],
              [InlineKeyboardButton("🔄 Rewrite", callback_data=f"regen:{ref}"),
               InlineKeyboardButton("✅ Mark as applied", callback_data=f"done:{ref}")]]
        await note.edit_text(preview + "\n\n<i>This job uses a website form. Copy the letter, "
                             "or use the DOCX below, and apply on the page.</i>",
                             parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        path = letter_docx(uid, job, letter)
        await q.message.reply_document(open(path, "rb"), filename=f"Anschreiben_{ref}.docx")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, refnr = q.data.split(":", 1)
    ud = ctx.user_data
    log.info("[tg] button %s %s from user %s", action, refnr, update.effective_user.id)
    job = ud.get("jobs", {}).get(refnr)
    if not job:
        await q.message.reply_text("That job is no longer in memory. Run /search again.")
        return
    if action in ("skip", "cancel"):
        ud.get("drafts", {}).pop(refnr, None)
        await q.edit_message_reply_markup(None)
    elif action in ("apply", "regen"):
        if refnr in ud.get("applied", []):
            await q.message.reply_text("Already applied to this one.")
            return
        if action == "regen":
            await q.edit_message_reply_markup(None)
        await make_draft(q, ctx, job)
    elif action == "done":
        ud.setdefault("applied", []).append(refnr)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("✅ Marked as applied.")
    elif action == "send":
        letter = ud.get("drafts", {}).get(refnr)
        if not letter or not job["email"]:
            await q.message.reply_text("No draft found. Press Apply again.")
            return
        if refnr in ud.get("applied", []):
            await q.message.reply_text("Already applied to this one.")
            return
        try:
            with step(f"send application {refnr}"):
                sent_to = await asyncio.to_thread(
                    send_application_sync, ud["profile"], job, Path(ud["cv_path"]), letter)
        except Exception as e:
            log.exception("send failed")
            await q.message.reply_text(f"Sending failed: {e}")
            return
        ud.setdefault("applied", []).append(refnr)
        await q.edit_message_reply_markup(None)
        note = " (DRY RUN: sent to yourself)" if DRY_RUN else ""
        await q.message.reply_text(f"✅ Application sent to {sent_to}{note}")

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


def main():
    persistence = PicklePersistence(filepath="botdata.pkl")
    app = Application.builder().token(TELEGRAM_TOKEN).persistence(persistence).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("location", location_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_error_handler(on_error)
    log.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()