"""Gemini: key pool, JSON calls, CV parsing, cover letters."""
import json, re, tempfile, threading, time
from pathlib import Path
from typing import Optional

import docx
import google.generativeai as genai
from google.api_core import exceptions as gexc

import os
from config import MODEL, DRY_RUN, log


# ------------------------------------------------------------------ key pool
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
            last_err = e
            log.warning("[gemini] %s: bad/empty response (%s) -> retry", tag, e)
            time.sleep(1)
    raise RuntimeError(f"Gemini failed after {max_attempts} attempts: {last_err}")


# ------------------------------------------------------------------ CV parsing
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
        data = gemini_json([PROFILE_PROMPT, part], label="parse-cv")
    else:
        d = docx.Document(str(path))
        text = "\n".join(p.text for p in d.paragraphs)
        for t in d.tables:
            for row in t.rows:
                text += "\n" + " | ".join(c.text for c in row.cells)
        data = gemini_json([PROFILE_PROMPT, text], label="parse-cv")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Gemini returned an unexpected CV structure")
    return data


# ------------------------------------------------------------------ cover letter
LETTER_PROMPT = """Write a German cover letter (Anschreiben) for this job application.
Return ONLY JSON: {{"subject": str, "body": str}}.
Rules:
- body starts with "Sehr geehrte Damen und Herren," and ends with "Mit freundlichen Grüßen" and the candidate's full name on the next line.
- 170-230 words, formal, concrete. Connect 2-3 REAL skills or experiences from the profile to the job requirements.
- Use ONLY facts from the candidate profile and the candidate note. Never invent employers, degrees, numbers, certificates,
  language levels, a start date, or a move to Germany. Do not mention missing requirements.
- The candidate note (if any) was written by the candidate. Work it in naturally where it fits (availability, relocation, etc.).
- subject: "Bewerbung als <job title> (Referenznummer <refnr>)".

CANDIDATE PROFILE:
{profile}

CANDIDATE NOTE:
{note}

JOB:
title: {title}
employer: {employer}
refnr: {refnr}
description: {description}
"""


def write_letter_sync(profile: dict, job: dict, note: Optional[str] = None) -> dict:
    prompt = LETTER_PROMPT.format(
        profile=json.dumps(profile, ensure_ascii=False),
        note=(note or "none"),
        title=job["title"], employer=job["employer"], refnr=job["refnr"],
        description=(job["description"] or "")[:3000],
    )
    data = gemini_json(prompt, label="cover-letter")
    if isinstance(data, list) and data:
        data = data[0]
    return {"subject": str(data["subject"]), "body": str(data["body"])}


def letter_docx(uid: int, job: dict, letter: dict) -> Path:
    d = docx.Document()
    d.add_paragraph(letter["subject"]).runs[0].bold = True
    for para in letter["body"].split("\n\n"):
        d.add_paragraph(para)
    clean_refnr = re.sub(r"[^\w-]", "_", job["refnr"])
    path = Path(tempfile.gettempdir()) / f"letter_{uid}_{clean_refnr}.docx"
    d.save(str(path))
    return path