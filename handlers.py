"""Telegram commands and buttons. All state lives in Supabase."""
import asyncio, html, re, tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import sheets
import db, mailer, storage, crypto
from db import arun, now_iso
from jobs import find_jobs_cached
from llm import parse_cv_sync, write_letter_sync, letter_docx
from matching import quick_info
from autopilot import fmt_stats_line
from config import (MAX_SHOW, DRY_RUN, ALLOW_SHARED_SENDER, ADMIN_IDS, EMAIL_RE,
                    CANCEL_WINDOW_MIN, WORK_START, WORK_END, log, step)

DONE_STATES = ("sent", "queued", "sending", "applied_manual")
STATUS_ICON = {"draft": "📝", "queued": "⏳", "sending": "📤", "sent": "✅", "failed": "⚠️",
               "cancelled": "✖", "applied_manual": "✅"}
import math
from telegram import BotCommand, BotCommandScopeChat

def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))

B = InlineKeyboardButton
ADMIN_PAGE = 8
CITIES = ["Berlin", "Hamburg", "München", "Köln", "Frankfurt", "Stuttgart",
          "Düsseldorf", "Dortmund", "Essen", "Leipzig", "Bremen", "Dresden",
          "Hannover", "Nürnberg"]

MAX_DAILY = 30


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [B("🔎 Search jobs", callback_data="menu:search"), B("👤 Profile", callback_data="menu:profile")],
        [B("📧 Email sender", callback_data="menu:sender"), B("🤖 Auto mode", callback_data="menu:auto")],
        [B("📍 Location", callback_data="menu:location"), B("📊 Stats", callback_data="menu:stats")],
        [B("🕘 History", callback_data="menu:history"), B("📝 Note", callback_data="menu:note")],
        [B("🗑 Delete my data", callback_data="menu:delete")],
    ]
    if is_admin(uid):
        rows.append([B("👥 Clients", callback_data="adm:open"), B("📊 Sheet", callback_data="adm:sheet")])
    return InlineKeyboardMarkup(rows)


def city_kb(current) -> InlineKeyboardMarkup:
    rows, row = [], []
    for c in CITIES:
        mark = "✅ " if current and current.lower() == c.lower() else ""
        row.append(B(mark + c, callback_data=f"loc:{c}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B(("✅ " if not current else "") + "🇩🇪 All of Germany", callback_data="loc:clear")])
    return InlineKeyboardMarkup(rows)


def auto_kb(user: dict) -> InlineKeyboardMarkup:
    on = bool(user.get("auto_on"))
    return InlineKeyboardMarkup([
        [B("🔴 Turn OFF" if on else "🟢 Turn ON", callback_data=f"autoset:{'off' if on else 'on'}")],
        [B(("✅ " if user.get("daily_limit") == n else "") + f"{n}/day", callback_data=f"autoset:limit {n}")
         for n in (3, 5, 15, MAX_DAILY)],
    ])


PUBLIC_COMMANDS = [
    BotCommand("start", "Open the main menu"),
    BotCommand("search", "Find and rank jobs"),
    BotCommand("profile", "Show your parsed profile"),
    BotCommand("location", "Choose a search city"),
    BotCommand("sender", "Connect your email account"),
    BotCommand("auto", "Automatic applications"),
    BotCommand("stats", "Your application numbers"),
    BotCommand("history", "Your recent applications"),
    BotCommand("note", "Add context for your letters"),
    BotCommand("delete", "Delete all your data"),
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("admin", "Clients and auto-mode overview"),
    BotCommand("sheet", "Master Google Sheet"),
]
DESCRIPTION = ("I help you apply for jobs in Germany. Send your CV (PDF or DOCX) and I find matching "
               "jobs from the Bundesagentur für Arbeit, write an AI cover letter and send your "
               "application from your own email. Auto mode can apply for you within limits you set. "
               "Your CV is processed by Google's Gemini API.")
ABOUT = "Job search in Germany: CV → matched jobs → AI cover letters → sent from your own email."


async def setup_bot(app):
    """Run once at startup (post_init): sets the command list, description and about text."""
    bot = app.bot
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS)
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(ABOUT)
    except Exception:
        log.warning("[setup] could not set bot commands/description", exc_info=True)
    for aid in ADMIN_IDS:     # admins also see /admin and /sheet in their own command list
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(aid))
        except Exception:
            log.warning("[setup] could not set admin commands for %s (has not pressed /start yet?)", aid)

async def clear_kb(q):
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass


def format_profile(p: dict) -> str:
    return (
        f"👤 {p.get('full_name')}  ({p.get('seniority')}, {p.get('years_experience')} yrs)\n"
        f"📍 Current location: {p.get('location')}   🇩🇪 German: {p.get('german_level')}\n"
        f"🔎 Searching for: {', '.join(p.get('job_titles') or [])}\n"
        f"🛠 {', '.join((p.get('skills') or [])[:12])}"
    )


def sender_ok(user) -> bool:
    return db.has_sender(user) or DRY_RUN or ALLOW_SHARED_SENDER


# ------------------------------------------------------------------ simple commands
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    log.info("[tg] /start from user %s", uid)
    await update.effective_message.reply_text(
        "Hi! Send me your CV (PDF or DOCX) to begin, then use the buttons below.\n\n"
        "Privacy: your CV is sent to employers by email and processed by Google's Gemini API.",
        reply_markup=main_menu(uid))


async def profile_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await arun(db.get_user, update.effective_user.id)
    p = u and u.get("profile")
    await update.effective_message.reply_text(format_profile(p) if p else "Send your CV first.")


async def location_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        u = await arun(db.get_user, uid)
        cur = (u or {}).get("location")
        await update.effective_message.reply_text(
            f"📍 Search location: {cur or 'all of Germany'}\n\n"
            "Pick a city below, or type /location <city> for any other place.",
            reply_markup=city_kb(cur))
        return
    val = " ".join(ctx.args)[:80]
    if val.lower() in ("clear", "all", "germany", "deutschland"):
        await arun(db.update_user, uid, location=None)
        await update.effective_message.reply_text("Searching all of Germany.")
    else:
        await arun(db.update_user, uid, location=val)
        await update.effective_message.reply_text(f"Search location set to {val}")
    log.info("[tg] user %s location -> %s", uid, val)


async def on_location_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split(":", 1)[1]
    if val != "clear" and val not in CITIES:
        return
    loc = None if val == "clear" else val
    await arun(db.update_user, q.from_user.id, location=loc)
    log.info("[tg] user %s location -> %s", q.from_user.id, loc)
    try:
        await q.edit_message_text(f"📍 Search location: {loc or 'all of Germany'}\n\nRun /search to use it.",
                                  reply_markup=city_kb(loc))
    except Exception:
        pass


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    actions = {"search": search_cmd, "profile": profile_cmd, "sender": sender_cmd, "auto": auto_cmd,
               "location": location_cmd, "stats": stats_cmd, "history": history_cmd,
               "note": note_cmd, "delete": delete_cmd}
    fn = actions.get(q.data.split(":", 1)[1])
    if fn:
        ctx.args = []
        await fn(update, ctx)


async def on_auto_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await clear_kb(q)
    ctx.args = q.data.split(":", 1)[1].split()       # e.g. ["off"] or ["limit", "5"]
    await auto_cmd(update, ctx)
    if ctx.args[0] != "on":                          # "on" may start the consent flow
        ctx.args = ["status"]
        await auto_cmd(update, ctx)

async def note_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        u = await arun(db.get_user, uid)
        cur = (u or {}).get("note")
        await update.effective_message.reply_text(
            f"Current note: {cur}\n\nChange: /note <text>   Remove: /note clear" if cur else
            "No note set. Example:\n/note Available from 1 March, willing to relocate within Germany.")
        return
    text = " ".join(ctx.args).strip()
    if text.lower() == "clear":
        await arun(db.update_user, uid, note=None)
        await update.effective_message.reply_text("Note removed.")
        return
    if len(text) > 500:
        await update.effective_message.reply_text("Please keep the note under 500 characters.")
        return
    await arun(db.update_user, uid, note=text)
    await update.effective_message.reply_text("Saved. It will be used in new letters.")


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await arun(db.history, update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No applications yet.")
        return
    lines = [f"{STATUS_ICON.get(r['status'], '•')} {esc(r['title'])} · {esc(r['employer'])} "
             f"<i>({r['status']}{', auto' if r['mode'] == 'auto' else ''})</i>" for r in rows]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ------------------------------------------------------------------ sender (own mailbox)
async def sender_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat = update.effective_chat.id
    args = ctx.args or []
    secret = bool(args) and args[0].lower() not in ("test", "clear")
    if secret:   # the message contains a password: remove it from the chat right away
        try:
            await update.message.delete()
        except Exception:
            pass

    async def say(text):
        await ctx.bot.send_message(chat, text)

    user = await arun(db.get_user, uid)

    if not args:
        if db.has_sender(user):
            await say(f"Sender: {user['smtp_user']} via {user['smtp_host']}:{user['smtp_port']}\n\n"
                      "/sender test - send a test mail to yourself\n/sender clear - disconnect")
        else:
            await say("No sender connected. Applications are sent from YOUR email address, "
                      "so replies reach you directly.\n\n"
                      "Gmail: enable 2-step verification, then create an App Password "
                      "(Google Account > Security > App passwords).\n"
                      "Do NOT use your normal password.\n\n"
                      "/sender you@gmail.com abcd efgh ijkl mnop\n"
                      "Other providers: /sender you@example.de password smtp.example.de 587\n\n"
                      "I delete your message right after reading it and store the password encrypted.")
        return

    if args[0].lower() == "clear":
        await arun(db.clear_sender, uid)
        await say("Sender removed. Auto mode will not send until you connect one again.")
        return

    if args[0].lower() == "test":
        if not user or not sender_ok(user):
            await say("No sender connected. Use /sender first.")
            return
        try:
            addr = await asyncio.to_thread(mailer.send_test, user)
            await say(f"✅ Test mail sent to {addr}. Check your inbox.")
        except Exception as e:
            await say(f"Test failed: {e}")
        return

    email, rest = args[0].strip().lower(), args[1:]
    host = port = None
    if len(rest) >= 3 and rest[-1].isdigit():
        port, host, rest = int(rest[-1]), rest[-2], rest[:-2]
    password = "".join(rest)
    if not EMAIL_RE.fullmatch(email) or not password:
        await say("Usage: /sender you@gmail.com <app password>")
        return
    if host is None:
        guess = mailer.guess_smtp(email)
        if not guess:
            await say("I don't know this provider. Use:\n/sender you@example.de password smtp.example.de 587")
            return
        host, port = guess
    try:
        await asyncio.to_thread(mailer.verify, host, port, email, password)
    except Exception as e:
        log.warning("[sender] verify failed for user %s: %s", uid, type(e).__name__)
        await say("❌ Login failed. Check the app password, and that SMTP/app passwords are enabled for "
                  f"this account ({type(e).__name__}).")
        return
    name = ((user or {}).get("profile") or {}).get("full_name")
    await arun(db.set_sender, uid, host, port, email, crypto.encrypt(password), name)
    log.info("[sender] user %s connected %s", uid, email)
    await say(f"✅ Connected: {email}. Applications will be sent from this address.\n"
              "Try /sender test to receive a test mail.")

async def sheet_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.effective_message.reply_text("Not allowed.")
        return
    if not sheets.enabled():
        await update.effective_message.reply_text("Google Sheets is not configured (check GOOGLE_SA_FILE and MASTER_SHEET_ID).")
        return
    args = ctx.args or []
    if args and args[0].lower() == "sync":
        n = await asyncio.to_thread(sheets.sync_user, uid)
        await update.effective_message.reply_text(f"Synced {n} application(s).")
        return
    if args and EMAIL_RE.fullmatch(args[0].strip().lower()):
        try:
            await asyncio.to_thread(sheets.share, args[0].strip().lower())
        except Exception as e:
            log.exception("[sheets] share failed")
            await update.effective_message.reply_text(f"Could not share: {e}")
            return
        await update.effective_message.reply_text(f"✅ Shared with {args[0]}")
        return
    await update.effective_message.reply_text(
        f"📊 Master sheet (one tab per sender email):\n{sheets.url()}\n\n"
        "/sheet sync - rebuild your rows\n/sheet name@gmail.com - share the sheet")

    
# ------------------------------------------------------------------ stats / admin
async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await arun(db.client_stats, update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No data yet.")
        return
    r = rows[0]
    await update.effective_message.reply_text(
        f"📊 Sent today: {r['sent_today']}\nSent total: {r['sent_total']}\nQueued: {r['queued']}\n"
        f"Failed: {r['failed']}\nWebsite applications (marked): {r['web_applied']}")


async def _admin_list(page: int):
    rows = await arun(db.client_stats)
    if not rows:
        return "No clients yet.", InlineKeyboardMarkup([[B("🔄 Refresh", callback_data="adm:list:0")]])
    rows.sort(key=lambda r: (not r["auto_on"], -(r["sent_today"] or 0), str(r.get("name") or "")))
    pages = max(1, math.ceil(len(rows) / ADMIN_PAGE))
    page = min(max(page, 0), pages - 1)
    on = sum(1 for r in rows if r["auto_on"])
    today = sum(r["sent_today"] or 0 for r in rows)
    text = (f"👥 <b>{len(rows)}</b> client(s) · 🟢 auto ON: <b>{on}</b> · ✉️ sent today: <b>{today}</b>\n\n"
            "Tap a client for details.")
    kb = []
    for r in rows[page * ADMIN_PAGE:(page + 1) * ADMIN_PAGE]:
        name = (r.get("name") or str(r["tg_id"]))[:20]
        label = (f"{'🟢' if r['auto_on'] else '⚪'} {name} · auto {'ON' if r['auto_on'] else 'off'}"
                 f" · {r['sent_today'] or 0} today")
        kb.append([B(label, callback_data=f"adm:c:{r['tg_id']}")])
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", callback_data=f"adm:list:{page - 1}"))
    nav.append(B(f"🔄 {page + 1}/{pages}", callback_data=f"adm:list:{page}"))
    if page < pages - 1:
        nav.append(B("Next ➡️", callback_data=f"adm:list:{page + 1}"))
    kb.append(nav)
    return text, InlineKeyboardMarkup(kb)


async def _client_card(tg_id: int):
    rows = await arun(db.client_stats, tg_id)
    back = [B("⬅️ Back to clients", callback_data="adm:list:0")]
    if not rows:
        return "Client not found.", InlineKeyboardMarkup([back])
    r = rows[0]
    u = await arun(db.get_user, tg_id) or {}
    text = (fmt_stats_line(r) +
            f"\n   limit {u.get('daily_limit')}/day · min score {u.get('min_score')} · "
            f"📍 {esc(u.get('location') or 'all of Germany')}")
    kb = [[B("📄 Latest applications", callback_data=f"adm:a:{tg_id}")]]
    if r["auto_on"]:
        kb.append([B("⏸ Turn auto OFF", callback_data=f"adm:off:{tg_id}")])
    kb.append(back)
    return text, InlineKeyboardMarkup(kb)


async def _client_apps(tg_id: int):
    rows = await arun(db.history, tg_id, 15)
    lines = []
    for r in rows:
        line = (f"{STATUS_ICON.get(r['status'], '•')} {esc(r['title'])} · {esc(r['employer'])}\n"
                f"   {r['status']}{' (auto)' if r['mode'] == 'auto' else ''} → {esc(r.get('to_email'))}")
        if r.get("error"):
            line += f"\n   ⚠️ {esc(str(r['error'])[:120])}"
        lines.append(line)
    text = "\n".join(lines)[:4000] if lines else "No applications for this client."
    return text, InlineKeyboardMarkup([[B("⬅️ Back", callback_data=f"adm:c:{tg_id}")]])


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Not allowed.")
        return
    if ctx.args and ctx.args[0].isdigit():            # /admin <tg_id> opens that client directly
        text, kb = await _client_card(int(ctx.args[0]))
    else:
        text, kb = await _admin_list(0)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Not allowed.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    kind = parts[1]
    if kind == "sheet":
        await q.message.reply_text(f"📊 Master sheet:\n{sheets.url()}" if sheets.enabled()
                                   else "Google Sheets is not configured.")
        return
    if kind == "open":                                # from the /start menu: send a new message
        text, kb = await _admin_list(0)
        await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return
    if kind == "list":
        text, kb = await _admin_list(int(parts[2]))
    elif kind == "c":
        text, kb = await _client_card(int(parts[2]))
    elif kind == "a":
        text, kb = await _client_apps(int(parts[2]))
    elif kind == "off":
        cid = int(parts[2])
        await arun(db.update_user, cid, auto_on=False)
        log.info("[admin] %s turned auto OFF for %s", q.from_user.id, cid)
        try:
            await ctx.bot.send_message(cid, "⏸ An admin turned your auto mode off.")
        except Exception:
            pass
        text, kb = await _client_card(cid)
    else:
        return
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        pass                                          # "message is not modified" on Refresh

# ------------------------------------------------------------------ CV upload
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    ext = Path(doc.file_name or "").suffix.lower()
    log.info("[tg] document from user %s: %s", uid, doc.file_name)
    if ext not in (".pdf", ".docx"):
        await update.effective_message.reply_text("Please send a PDF or DOCX file.")
        return
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.effective_message.reply_text("File too large (max 10 MB).")
        return
    msg = await update.effective_message.reply_text("📥 Downloading your CV...")
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"cv{ext}"
            with step("download CV"):
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(path)
            data = path.read_bytes()
            await msg.edit_text("🧠 Reading your CV with AI...")
            with step("parse CV with Gemini"):
                profile = await asyncio.to_thread(parse_cv_sync, path)
        with step("upload CV to storage"):
            key = await asyncio.to_thread(storage.upload_cv, uid, data, ext)
    except Exception as e:
        log.exception("CV processing failed")
        await msg.edit_text(f"Could not process the CV: {e}")
        return
    await arun(db.update_user, uid, profile=profile, cv_path=key)
    log.info("[profile] %s | titles=%s", profile.get("full_name"), profile.get("job_titles"))
    tip = "" if sender_ok(await arun(db.get_user, uid)) else \
        "\n\n📧 Next: connect your email with /sender so applications go out from your own address."
    await msg.edit_text(format_profile(profile) + "\n\nLooks right? Run /search" + tip)


# ------------------------------------------------------------------ search
async def search_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await arun(db.get_user, uid)
    profile = user and user.get("profile")
    if not profile:
        await update.effective_message.reply_text("Send your CV first.")
        return
    where = user.get("location")
    titles = profile.get("job_titles") or []
    log.info("[search] user=%s where=%s titles=%s", uid, where or "Germany", titles)
    msg = await update.effective_message.reply_text(f"🔎 Searching jobs in {where or 'all of Germany'}...")
    try:
        jobs = await find_jobs_cached(titles, where)
        known = set(await arun(db.known_refnrs, uid))
        jobs = [j for j in jobs if j["refnr"] not in known]
    except Exception as e:
        log.exception("search failed")
        await msg.edit_text(f"Search failed: {e}")
        return
    if not jobs:
        await msg.edit_text("No new jobs found. Try another city with /location or /location clear.")
        return

    for j in jobs:
        quick_info(profile, j)
    jobs.sort(key=lambda j: j["score"], reverse=True)
    await msg.edit_text(f"📋 Found {len(jobs)} jobs, showing {min(len(jobs), MAX_SHOW)}:")

    for job in jobs[:MAX_SHOW]:
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
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup([row]),
                                        disable_web_page_preview=True)
        await asyncio.sleep(0.4)  # stay under Telegram's flood limits
    log.info("[search] showed %d jobs to user %s", min(len(jobs), MAX_SHOW), uid)


# ------------------------------------------------------------------ auto command
async def auto_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await arun(db.get_user, uid)
    sub = (ctx.args[0].lower() if ctx.args else "status")

    if sub == "status" or not user:
        if not user:
            await update.effective_message.reply_text("Send your CV first.")
            return
        today = await arun(db.count_auto_today, uid)
        await update.effective_message.reply_text(
            f"Auto mode: {'ON' if user['auto_on'] else 'OFF'}\n"
            f"Sender: {user.get('smtp_user') or 'not connected (/sender)'}\n"
            f"Daily limit: {user['daily_limit']} (used today: {today})\n"
            f"Minimum match score: {user['min_score']}\n"
            f"Location: {user.get('location') or 'all of Germany'}\n\n"
            "/auto on | off | limit N (1-10) | score N")
        return

    if sub == "on":
        if not user.get("profile") or not user.get("cv_path"):
            await update.effective_message.reply_text("Send your CV first.")
            return
        if not sender_ok(user):
            await update.effective_message.reply_text("Connect your email first with /sender.")
            return
        if not user.get("consent_at"):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I agree", callback_data="autoconsent:yes"),
                                        InlineKeyboardButton("No", callback_data="autoconsent:no")]])
            await update.effective_message.reply_text(
                "Auto mode applies to jobs for you without asking each time.\n\n"
                "• Your CV and cover letter are emailed to employers from your email address.\n"
                "• Your CV is processed by Google's Gemini API.\n"
                f"• You get a Telegram message with a Cancel button; mails wait at least {CANCEL_WINDOW_MIN} minutes "
                f"and go out only Mon-Fri {WORK_START}:00-{WORK_END}:00 (Berlin).\n"
                "• Letters are AI-written and not reviewed by a human before sending. They may contain mistakes.\n"
                "• /delete removes all your data at any time.\n\n"
                "Do you agree?", reply_markup=kb)
            return
        await arun(db.update_user, uid, auto_on=True)
        await update.effective_message.reply_text("🤖 Auto mode ON." + (" (DRY RUN: mails go to yourself)" if DRY_RUN else ""))
    elif sub == "off":
        await arun(db.update_user, uid, auto_on=False)
        await update.effective_message.reply_text("Auto mode OFF. Already queued letters can still be cancelled with their button.")
    elif sub in ("limit", "score") and len(ctx.args) > 1 and ctx.args[1].isdigit():
        n = int(ctx.args[1])
        if sub == "limit":
            if not 1 <= n <= MAX_DAILY:
                await update.effective_message.reply_text(f"Limit must be between 1 and {MAX_DAILY}.")
                return
            await arun(db.update_user, uid, daily_limit=n)
        else:
            if not 0 <= n <= 30:
                await update.effective_message.reply_text(f"Score must be between 0 and 30.")
                return
            await arun(db.update_user, uid, min_score=n)
        await update.effective_message.reply_text(f"Saved: {sub} = {n}")
    else:
        await update.effective_message.reply_text("Usage: /auto on | off | limit N | score N | status")


# ------------------------------------------------------------------ delete
async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Yes, delete everything", callback_data="delete:yes"),
                                InlineKeyboardButton("Cancel", callback_data="delete:no")]])
    await update.effective_message.reply_text(
        "This permanently deletes your CV, profile, email connection, settings, drafts and application history. "
        "Emails already sent to employers cannot be recalled.", reply_markup=kb)


async def wipe_user(uid: int):
    try:
        await asyncio.to_thread(sheets.remove_user_rows, uid)
    except Exception:
        log.exception("[gdpr] sheet rows cleanup failed for %s", uid)
    try:
        await asyncio.to_thread(storage.delete_user_files, uid)
    except Exception:
        log.exception("[gdpr] storage cleanup failed for %s", uid)
    await arun(db.delete_user, uid)
    log.info("[gdpr] deleted all data of user %s", uid)


# ------------------------------------------------------------------ drafts and buttons
async def make_draft(q, job: dict):
    uid = q.from_user.id
    user = await arun(db.get_user, uid)
    if not user or not user.get("profile"):
        await q.message.reply_text("Send your CV first.")
        return
    note = await q.message.reply_text("✍️ Writing your cover letter...")
    try:
        with step(f"write cover letter {job['refnr']}"):
            letter = await asyncio.to_thread(write_letter_sync, user["profile"], job, user.get("note"))
    except Exception as e:
        log.exception("letter failed")
        await note.edit_text(f"Could not write the letter: {e}")
        return
    await arun(db.upsert_app, uid, job, status="draft", mode="manual", letter=letter, error=None)
    ref = job["refnr"]
    preview = f"<b>{esc(letter['subject'])}</b>\n\n{esc(letter['body'])}"
    if job.get("email"):
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
        path = await asyncio.to_thread(letter_docx, uid, job, letter)
        try:
            with open(path, "rb") as fh:
                await q.message.reply_document(fh, filename=f"Anschreiben_{ref}.docx")
        finally:
            path.unlink(missing_ok=True)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    action, arg = q.data.split(":", 1)
    log.info("[tg] button %s %s from user %s", action, arg, uid)

    # ---- consent / delete
    if action == "autoconsent":
        await clear_kb(q)
        if arg == "yes":
            await arun(db.update_user, uid, consent_at=now_iso(), auto_on=True)
            await q.message.reply_text("🤖 Auto mode ON. Consent saved." +
                                       (" (DRY RUN: mails go to yourself)" if DRY_RUN else ""))
        else:
            await q.message.reply_text("Okay, auto mode stays off.")
        return
    if action == "delete":
        await clear_kb(q)
        if arg == "yes":
            await wipe_user(uid)
            await q.message.reply_text("🗑 All your data was deleted. Send a CV to start again.")
        else:
            await q.message.reply_text("Cancelled. Nothing was deleted.")
        return

    refnr = arg

    # ---- cancel (draft or auto-queued): no job lookup needed
    if action == "cancel":
        app = await arun(db.get_app, uid, refnr)
        await clear_kb(q)
        if not app:
            return
        if app["status"] == "draft":
            await arun(db.delete_app, uid, refnr)
            await q.message.reply_text("Cancelled.")
        elif app["status"] == "queued":
            ok = await arun(db.set_status, uid, refnr, "cancelled", ["queued"])
            if ok:
                sheets.fire(uid, refnr) 
            await q.message.reply_text("✖ Cancelled. It will not be sent." if ok
                                       else "Too late, it is already being sent.")
        else:
            await q.message.reply_text(f"Cannot cancel: status is '{app['status']}'.")
        return

    job = await arun(db.get_job, refnr)
    if not job:
        await q.message.reply_text("That job is no longer available. Run /search again.")
        return
    app = await arun(db.get_app, uid, refnr)

    if action == "skip":
        await clear_kb(q)
        if not app or app["status"] == "draft":
            await arun(db.upsert_app, uid, job, status="skipped", mode="manual")

    elif action in ("apply", "regen"):
        if app and app["status"] in DONE_STATES:
            await q.message.reply_text("Already applied to this one.")
            return
        if action == "regen":
            await clear_kb(q)
        await make_draft(q, job)

    elif action == "done":
        ok = await arun(db.set_status, uid, refnr, "applied_manual", ["draft"])
        if ok:
            sheets.fire(uid, refnr)   
        await clear_kb(q)
        await q.message.reply_text("✅ Marked as applied." if ok else "No draft found.")

    elif action == "send":
        if not app or app["status"] != "draft" or not app.get("letter") or not job.get("email"):
            await q.message.reply_text("No draft found. Press Apply again.")
            return
        user = await arun(db.get_user, uid)
        if not sender_ok(user):
            await q.message.reply_text("Connect your email first with /sender, then press Send again.")
            return
        claimed = await arun(db.set_status, uid, refnr, "sending", ["draft"])   # blocks double-click
        sheets.fire(uid, refnr)
        if not claimed:
            await q.message.reply_text("This one is already being sent.")
            return
        try:
            with step(f"send application {refnr}"):
                cv = await asyncio.to_thread(storage.download_cv, user["cv_path"])
                sent_to = await asyncio.to_thread(mailer.send, user, job, app["letter"], cv,
                                                  Path(user["cv_path"]).suffix)
        except Exception as e:
            log.exception("send failed")
            await arun(db.set_status, uid, refnr, "draft", ["sending"], error=str(e)[:500])
            await q.message.reply_text(f"Sending failed: {e}")
            return
        await arun(db.set_status, uid, refnr, "sent", ["sending"],
                   sent_at=now_iso(), error=None, to_email=job["email"])
        await clear_kb(q)
        note = " (DRY RUN: sent to yourself)" if DRY_RUN else ""
        await q.message.reply_text(f"✅ Application sent to {sent_to}{note}")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)