"""Telegram commands and buttons. All state lives in Supabase."""
import asyncio, html, re, tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import sheets
import db, mailer, storage, crypto
from db import arun, now_iso
from jobs import find_jobs_cached
from llm import parse_cv_sync, write_letter_sync, letter_docx, GeminiBusy, letter_lang, LETTER_LANGS
from matching import quick_info
from autopilot import fmt_stats_line
from config import (MAX_SHOW, DRY_RUN, ALLOW_SHARED_SENDER, ADMIN_IDS, EMAIL_RE,
                    CANCEL_WINDOW_MIN, WORK_START, WORK_END, MAX_PROFILES, USER_WAIT_S, log, step)
import autopilot
from autopilot import cb, job_line
import replies
import letters
from collections import defaultdict
DONE_STATES = ("sent", "queued", "sending", "applied_manual")
STATUS_ICON = {"draft": "📝", "queued": "⏳", "sending": "📤", "sent": "✅", "failed": "⚠️",
               "cancelled": "✖", "applied_manual": "✅"}
OUTCOME_ICON = {"interview": "🎉", "rejected": "❌", "acknowledged": "📨", "other": "✉️", "bounced": "⚠️"}
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


# ------------------------------------------------------------------ active profile
async def owned_profile(owner: int, pid: int):
    """The profile row if `owner` may manage it, else None."""
    p = await arun(db.get_user, pid)
    if p and (p["tg_id"] == owner or p.get("owner_id") == owner):
        return p
    return None


async def current_pid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Profile that commands act on: the owner's active profile, default their own (= Telegram id)."""
    owner = update.effective_user.id
    pid = ctx.user_data.get("pid")
    if pid is None:
        o = await arun(db.get_user, owner)
        pid = (o or {}).get("active_profile") or owner
    if pid != owner and not await owned_profile(owner, pid):
        pid = owner                                   # profile was deleted
    ctx.user_data["pid"] = pid
    return pid


async def set_active(owner: int, pid: int, ctx) -> None:
    await arun(db.update_user, owner, active_profile=pid)
    ctx.user_data["pid"] = pid


from telegram.error import NetworkError, TimedOut

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):   # BadGateway is a NetworkError
        log.warning("[tg] transient network error: %s", err)
        return
    log.error("Unhandled error", exc_info=err)

def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [B("👥 Profiles", callback_data="prof:list"), B("🔎 Search jobs", callback_data="menu:search")],
        [B("📧 Email sender", callback_data="menu:sender"), B("🤖 Auto mode", callback_data="menu:auto")],
        [B("📍 Location", callback_data="menu:location"), B("📊 Stats", callback_data="menu:stats")],
        [B("🕘 History", callback_data="menu:history"), B("📝 Note", callback_data="menu:note")],

        [B("📬 Replies", callback_data="menu:replies"), B("⚡ Run now", callback_data="menu:run")],
        [B("📎 Attachments", callback_data="menu:files"), B("🌐 Letter language", callback_data="menu:language")],
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
    BotCommand("profiles", "All candidate profiles: switch, auto on/off, limits"),
    BotCommand("profile", "Show the active profile"),
    BotCommand("location", "Choose a search city"),
    BotCommand("sender", "Connect your email account"),
    BotCommand("auto", "Automatic applications"),
    BotCommand("stats", "Your application numbers"),
    BotCommand("history", "Your recent applications"),
    BotCommand("note", "Add context for your letters"),
    BotCommand("run", "Run a scan and send applications immediately"),
    BotCommand("replies", "Track employer replies in your inbox"),
    BotCommand("files", "Certificates and references sent with applications"),
    BotCommand("language", "Cover letter language: German or English"),
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



async def run_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = await current_pid(update, ctx)
    user = await arun(db.get_user, uid)
    if not user or not user.get("profile") or not user.get("cv_path"):
        await update.effective_message.reply_text("Send your CV first.")
        return
    if not sender_ok(user):
        await update.effective_message.reply_text("Connect your email first with /sender.")
        return
    kb = InlineKeyboardMarkup([[B("⚡ Yes, scan and send now", callback_data="run:yes"),
                                B("Cancel", callback_data="run:no")]])
    await update.effective_message.reply_text(
        f"This scans now and sends up to {user['daily_limit']} real applications within a minute or two, "
        "ignoring the cancel window and work hours. Your daily limit still applies.",
        reply_markup=kb)


async def on_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await clear_kb(q)
    if q.data != "run:yes":
        return
    uid = await current_pid(update, ctx)
    user = await arun(db.get_user, uid)
    if not user or not user.get("profile"):
        return
    msg = await q.message.reply_text("⚡ Scanning...")
    try:
        st = await autopilot.scan_user(ctx, user, immediate=True)
    except Exception as e:
        log.exception("[run] scan failed")
        await msg.edit_text(f"Scan failed: {e}")
        return
    await msg.edit_text(
        f"Jobs found: {st['total']}\n"
        f"Already seen: {st['known']} · no email: {st['no_email']} · employer cooldown: {st['cooldown']}"
        + (f" · letter failed: {st['letter_failed']}" if st["letter_failed"] else "")
        + f"\n✅ Queued: {st['queued']}" + (f" — sending now..." if st["queued"] else "")
        + (f"\nℹ️ {st['note']}" if st["note"] else ""))
    if st.get("refnrs"):
        await autopilot.send_now(ctx, uid, st["refnrs"])


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
    owner = update.effective_user.id
    pid = await current_pid(update, ctx)
    log.info("[tg] /start from user %s (profile %s)", owner, pid)
    active = await arun(db.get_user, pid)
    await update.effective_message.reply_text(
        "Hi! Send me your CV (PDF or DOCX) to begin, then use the buttons below.\n"
        f"⭐ Active profile: {db.profile_name(active)} (switch in 👥 Profiles)\n\n"
        "Privacy: your CV is sent to employers by email and processed by Google's Gemini API.",
        reply_markup=main_menu(owner))


async def profile_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await arun(db.get_user, await current_pid(update, ctx))
    p = u and u.get("profile")
    await update.effective_message.reply_text(format_profile(p) if p else "Send your CV first.")


async def location_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = await current_pid(update, ctx)
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
    pid = await current_pid(update, ctx)
    await arun(db.update_user, pid, location=loc)
    log.info("[tg] user %s location -> %s", pid, loc)
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
               "note": note_cmd, "delete": delete_cmd, "run": run_cmd, "replies": replies_cmd, "files": files_cmd,
               "language": language_cmd}
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
    uid = await current_pid(update, ctx)
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
    rows = await arun(db.history, await current_pid(update, ctx))
    if not rows:
        await update.effective_message.reply_text("No applications yet.")
        return
    lines = [f"{STATUS_ICON.get(r['status'], '•')} {esc(r['title'])} · {esc(r['employer'])} "
             f"<i>({r['status']}{', auto' if r['mode'] == 'auto' else ''})</i>"
             + (f" {OUTCOME_ICON.get(r.get('outcome'), '')} {r['outcome']}" if r.get("outcome") else "")
             for r in rows]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ------------------------------------------------------------------ sender (own mailbox)
async def sender_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = await current_pid(update, ctx)
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
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Not allowed.")
        return
    if not sheets.enabled():
        await update.effective_message.reply_text("Google Sheets is not configured (check GOOGLE_SA_FILE and MASTER_SHEET_ID).")
        return
    args = ctx.args or []
    if args and args[0].lower() == "sync":
        n = await asyncio.to_thread(sheets.sync_user, await current_pid(update, ctx))
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

    
# ------------------------------------------------------------------ reply tracking
HOST_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def _replies_summary(st: dict) -> str:
    if st.get("error"):
        return f"⚠️ Check failed: {st['error']}"
    if not st["apps"]:
        return "No sent applications in the last 60 days, nothing to match yet."
    if not st["new"]:
        return f"No new replies (watching {st['apps']} sent application(s))."
    parts = [f"{OUTCOME_ICON[k]} {k}: {st[k]}" for k in OUTCOME_ICON if st.get(k)]
    return f"📬 {st['new']} new repl{'y' if st['new'] == 1 else 'ies'}: " + ", ".join(parts)


async def replies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = await current_pid(update, ctx)
    say = update.effective_message.reply_text
    user = await arun(db.get_user, uid)
    args = ctx.args or []
    sub = args[0].lower() if args else ""
    if not db.has_sender(user):
        await say("Connect your own email first with /sender. Replies are read from that mailbox.")
        return
    host = replies.imap_host_for(user)

    if sub == "on":
        try:
            await asyncio.to_thread(replies.verify_imap, host, user["smtp_user"],
                                    crypto.decrypt(user["smtp_pass_enc"]))
        except Exception as e:
            log.warning("[replies] IMAP verify failed for user %s: %s", uid, type(e).__name__)
            await say(f"❌ Could not log in to {host} ({type(e).__name__}).\n"
                      "GMX / web.de: enable IMAP in the mailbox settings first.\n"
                      "Other provider: /replies imap imap.example.de")
            return
        await arun(db.update_user, uid, track_replies=True, imap_error=None)
        msg = await say("📬 Reply tracking ON. Checking your inbox now...")
        user["track_replies"] = True
        st = await replies.check_user(ctx, user, notify_login_error=False)
        await msg.edit_text("📬 Reply tracking ON. I check your inbox every 30 minutes.\n\n"
                            + _replies_summary(st))
        return
    if sub == "off":
        await arun(db.update_user, uid, track_replies=False)
        await say("Reply tracking OFF. I no longer read your inbox.")
        return
    if sub == "check":
        if not user.get("track_replies"):
            await say("Reply tracking is off. Turn it on with /replies on")
            return
        msg = await say("📬 Checking your inbox...")
        st = await replies.check_user(ctx, user, notify_login_error=False)
        await msg.edit_text(_replies_summary(st))
        return
    if sub == "imap" and len(args) > 1:
        new_host = args[1].strip().lower()
        if not HOST_RE.fullmatch(new_host):
            await say("Usage: /replies imap imap.example.de")
            return
        await arun(db.update_user, uid, imap_host=new_host)
        await say(f"IMAP server set to {new_host}. Now turn tracking on with /replies on")
        return

    on = bool(user.get("track_replies"))
    checked = str(user.get("imap_checked_at") or "")[:16].replace("T", " ") or "never"
    text = (f"📬 Reply tracking: {'ON' if on else 'OFF'}\n"
            f"Mailbox: {user['smtp_user']} (IMAP {host})\n"
            + (f"Last check: {checked} UTC\n" if on else "")
            + (f"⚠️ Last error: {user['imap_error']}\n" if user.get("imap_error") else "")
            + "\nWhen ON, I look at the sender and subject of new mails in this inbox (read-only: "
              "nothing is marked, moved or deleted). Only replies that match one of your applications "
              "are opened and summarized with Google's Gemini. You get a message for interviews, "
              "rejections and bounced addresses, and your sheet's Outcome column updates itself.")
    cv_mail = ((user.get("profile") or {}).get("email") or "").strip().lower()
    if cv_mail and cv_mail != user["smtp_user"].lower():
        text += (f"\n\nℹ️ Your CV lists {cv_mail}. With tracking ON, new applications ask employers "
                 f"to reply to {user['smtp_user']} instead, so I can see the replies.")
    text += "\n\nOther IMAP server: /replies imap imap.example.de"
    kb = ([[B("🔴 Turn OFF", callback_data="replies:off"), B("🔄 Check now", callback_data="replies:check")]]
          if on else [[B("🟢 Turn ON", callback_data="replies:on")]])
    await say(text, reply_markup=InlineKeyboardMarkup(kb))


async def on_replies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await clear_kb(q)
    ctx.args = [q.data.split(":", 1)[1]]
    await replies_cmd(update, ctx)


# ------------------------------------------------------------------ attachments (/files)
_att_locks = defaultdict(asyncio.Lock)     # several files sent at once must not overwrite each other


def _kb_size(n) -> str:
    return f"{(n or 0) / 1048576:.1f} MB" if (n or 0) >= 1048576 else f"{(n or 0) // 1024} KB"


async def files_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, note: str = ""):
    uid = await current_pid(update, ctx)
    user = await arun(db.get_user, uid) or {}
    atts = user.get("attachments") or []
    text = ((note + "\n\n" if note else "")
            + "📎 <b>Attachments</b>, sent with every email application:\n"
            "1. Anschreiben (PDF, made automatically)\n2. Your CV\n"
            + ("\n".join(f"{i + 2}. {esc(a['name'])} ({_kb_size(a.get('size'))})" for i, a in enumerate(atts))
               if atts else "<i>No extra files yet.</i>")
            + "\n\nGerman employers usually expect certificates and references (Zeugnisse). "
            f"Up to {storage.ATT_MAX_FILES} files, PDF, JPG or PNG, {storage.ATT_MAX_FILE // 1048576} MB each.")
    kb = []
    if len(atts) < storage.ATT_MAX_FILES:
        kb.append([B("➕ Add files", callback_data="files:add")])
    kb += [[B(f"🗑 {a['name'][:40]}", callback_data=f"files:del:{a['id']}")] for a in atts]
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              reply_markup=InlineKeyboardMarkup(kb) if kb else None)


async def on_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = await current_pid(update, ctx)
    parts = q.data.split(":")
    if parts[1] == "add":
        await clear_kb(q)
        ctx.user_data["att_mode"] = True
        await q.message.reply_text(
            "Send the files now: PDF, JPG or PNG (photos are fine too). "
            "Press Done when you're finished. While this is open, files are NOT treated as a new CV.",
            reply_markup=InlineKeyboardMarkup([[B("✅ Done", callback_data="files:done")]]))
    elif parts[1] == "done":
        await clear_kb(q)
        ctx.user_data.pop("att_mode", None)
        await files_cmd(update, ctx)
    elif parts[1] == "del" and len(parts) > 2:
        await clear_kb(q)
        async with _att_locks[uid]:
            user = await arun(db.get_user, uid) or {}
            atts = user.get("attachments") or []
            gone = [a for a in atts if a["id"] == parts[2]]
            if gone:
                await arun(db.update_user, uid, attachments=[a for a in atts if a["id"] != parts[2]])
                try:
                    await asyncio.to_thread(storage.delete_keys, [gone[0]["key"]])
                except Exception:
                    log.warning("[files] storage delete failed for user %s", uid, exc_info=True)
        await files_cmd(update, ctx, note=f"🗑 Removed {esc(gone[0]['name'])}." if gone else "")


async def add_attachment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tg_obj, filename: str, size):
    uid = await current_pid(update, ctx)
    say = update.effective_message.reply_text
    ext = Path(filename).suffix.lower()
    if ext not in storage.ATT_MIME:
        await say("Only PDF, JPG or PNG files can be attached.")
        return
    if size and size > storage.ATT_MAX_FILE:
        await say(f"{filename} is too large (max {storage.ATT_MAX_FILE // 1048576} MB per file).")
        return
    async with _att_locks[uid]:
        user = await arun(db.get_user, uid)
        if not user:
            await say("Send your CV first, then add attachments.")
            ctx.user_data.pop("att_mode", None)
            return
        atts = user.get("attachments") or []
        if len(atts) >= storage.ATT_MAX_FILES:
            await say(f"You already have {storage.ATT_MAX_FILES} files. Remove one in /files first.")
            ctx.user_data.pop("att_mode", None)
            return
        try:
            tg_file = await tg_obj.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            if sum(a.get("size") or 0 for a in atts) + len(data) > storage.ATT_MAX_TOTAL:
                await say(f"Total attachments would exceed {storage.ATT_MAX_TOTAL // 1048576} MB. "
                          "Remove a file or send a smaller one.")
                return
            meta = await asyncio.to_thread(storage.upload_attachment, uid, data, filename)
        except Exception:
            log.exception("[files] upload failed for user %s", uid)
            await say(f"Could not save {filename}. Please try again.")
            return
        atts = atts + [meta]
        await arun(db.update_user, uid, attachments=atts)
    log.info("[files] user %s added attachment (%d/%d)", uid, len(atts), storage.ATT_MAX_FILES)
    left = storage.ATT_MAX_FILES - len(atts)
    if left <= 0:
        ctx.user_data.pop("att_mode", None)
        await say(f"✅ Added {filename}. That's the maximum of {storage.ATT_MAX_FILES} files.")
        await files_cmd(update, ctx)
    else:
        await say(f"✅ Added {filename} ({len(atts)}/{storage.ATT_MAX_FILES}). Send more or press Done above.")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("att_mode"):
        await update.effective_message.reply_text(
            "To attach a certificate, open 📎 Attachments (/files) and press Add files first.")
        return
    photo = update.message.photo[-1]                  # largest size
    n = len((await arun(db.get_user, await current_pid(update, ctx)) or {}).get("attachments") or []) + 1
    await add_attachment(update, ctx, photo, f"Dokument_{n}.jpg", photo.file_size)


# ------------------------------------------------------------------ profiles (👥)
def _langs(p: dict) -> str:
    prof = p.get("profile") or {}
    return f"🇩🇪 {prof.get('german_level') or '?'} · 🇬🇧 {prof.get('english_level') or '?'}"


async def _profiles_view(owner: int, active: int):
    rows = await arun(db.list_profiles, owner)
    lines, kb = [], []
    for p in rows:
        pid, star = p["tg_id"], "⭐ " if p["tg_id"] == active else ""
        today = await arun(db.count_auto_today, pid)
        name = db.profile_name(p)
        lines.append(
            f"{star}<b>{esc(name)}</b> · {_langs(p)}\n"
            f"   {'🟢 auto ON' if p.get('auto_on') else '⚪ auto off'} · {today}/{p.get('daily_limit')} today · "
            f"📍 {esc(p.get('location') or 'all DE')}\n"
            f"   ✍️ {LETTER_LANGS[letter_lang(p)]} letters · ✉️ {esc(p.get('smtp_user') or 'no sender')} · "
            f"📬 replies {'ON' if p.get('track_replies') else 'off'}"
            f"{'' if p.get('cv_path') else ' · ⚠️ no CV'}")
        kb.append([B(f"{star}{'🟢' if p.get('auto_on') else '⚪'} {name[:24]} · "
                     f"{(p.get('profile') or {}).get('german_level') or '?'} · {today}/{p.get('daily_limit')}",
                     callback_data=f"prof:open:{pid}")])
    if len(rows) < MAX_PROFILES:
        kb.append([B("➕ New profile", callback_data="prof:new"), B("🔄 Refresh", callback_data="prof:list")])
    else:
        kb.append([B("🔄 Refresh", callback_data="prof:list")])
    text = (f"👥 <b>Profiles</b> ({len(rows)}/{MAX_PROFILES})\n"
            "⭐ = active: CV upload, /search, /sender, /files and /replies apply to it.\n\n"
            + ("\n\n".join(lines) if lines else "<i>No profiles yet. Send a CV or tap ➕ New profile.</i>"))
    return text[:4000], InlineKeyboardMarkup(kb)


async def _profile_card(p: dict, active: int):
    pid = p["tg_id"]
    prof = p.get("profile") or {}
    today = await arun(db.count_auto_today, pid)
    on = bool(p.get("auto_on"))
    text = (f"{'⭐ ' if pid == active else ''}<b>{esc(db.profile_name(p))}</b>"
            f"{' (active)' if pid == active else ''}\n"
            f"{esc(prof.get('seniority') or '?')} · {esc(prof.get('years_experience') or '?')} yrs · {_langs(p)}\n"
            f"🔎 {esc(', '.join(prof.get('job_titles') or []) or '-')}\n"
            f"📍 Search: {esc(p.get('location') or 'all of Germany')}\n"
            f"📄 CV: {'yes' if p.get('cv_path') else 'missing'} · 📎 extra files: {len(p.get('attachments') or [])}\n"
            f"✍️ Letter language: {LETTER_LANGS[letter_lang(p)]}\n"
            f"✉️ Sender: {esc(p.get('smtp_user') or 'not connected')}\n"
            f"📬 Reply tracking: {'ON' if p.get('track_replies') else 'off'}\n"
            f"🤖 Auto: {'🟢 ON' if on else '⚪ off'} · {today}/{p.get('daily_limit')} today"
            + (f"\n📝 {esc(p['note'][:120])}" if p.get("note") else ""))
    kb = []
    if pid != active:
        kb.append([B("⭐ Make active", callback_data=f"prof:act:{pid}")])
    kb.append([B("🔴 Auto OFF" if on else "🟢 Auto ON", callback_data=f"prof:auto:{pid}:{'off' if on else 'on'}")])
    kb.append([B(("✅ " if p.get("daily_limit") == n else "") + f"{n}/day", callback_data=f"prof:lim:{pid}:{n}")
               for n in (3, 5, 15, MAX_DAILY)])
    kb.append([B(("✅ " if letter_lang(p) == code else "") + label, callback_data=f"prof:lang:{pid}:{code}")
               for code, label in LETTER_LANGS.items()])
    kb.append([B("⚡ Run now", callback_data=f"prof:run:{pid}"), B("🔎 Search", callback_data=f"prof:search:{pid}")])
    kb.append([B("📧 Sender", callback_data=f"prof:sender:{pid}"), B("📬 Replies", callback_data=f"prof:rep:{pid}"),
               B("📎 Files", callback_data=f"prof:files:{pid}")])
    kb.append([B("🗑 Delete profile", callback_data=f"prof:del:{pid}"), B("⬅️ All profiles", callback_data="prof:list")])
    return text[:4000], InlineKeyboardMarkup(kb)


async def profiles_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    owner = update.effective_user.id
    text, kb = await _profiles_view(owner, await current_pid(update, ctx))
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def on_profiles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    owner = q.from_user.id
    parts = q.data.split(":")
    kind = parts[1]
    active = await current_pid(update, ctx)

    async def show(text, kb):
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:                             # "not modified", or the message is too old to edit
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

    if kind == "list":
        await q.answer()
        await show(*await _profiles_view(owner, active))
        return
    if kind == "new":
        if len(await arun(db.list_profiles, owner)) >= MAX_PROFILES:
            await q.answer(f"Maximum {MAX_PROFILES} profiles.", show_alert=True)
            return
        await q.answer()
        pid = await arun(db.create_profile, owner)
        await set_active(owner, pid, ctx)
        log.info("[profiles] user %s created profile %s", owner, pid)
        await clear_kb(q)
        await q.message.reply_text("✅ New profile created and set as ⭐ active.\n\n"
                                   "1. Send this person's CV (PDF or DOCX) now.\n"
                                   "2. Connect their mailbox with /sender.\n"
                                   "3. Open 👥 Profiles to turn auto mode on.")
        return

    pid = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else None
    p = await owned_profile(owner, pid) if pid is not None else None
    if not p:
        await q.answer("Profile not found.", show_alert=True)
        await show(*await _profiles_view(owner, active))
        return

    if kind == "open":
        await q.answer()
        await show(*await _profile_card(p, active))
    elif kind == "act":
        await set_active(owner, pid, ctx)
        await q.answer(f"⭐ {db.profile_name(p)} is now active")
        await show(*await _profile_card(p, pid))
    elif kind == "auto" and parts[3] == "off":
        await arun(db.update_user, pid, auto_on=False)
        await q.answer("Auto mode OFF")
        await show(*await _profile_card(await arun(db.get_user, pid), active))
    elif kind == "lim" and parts[3].isdigit() and 1 <= int(parts[3]) <= MAX_DAILY:
        await arun(db.update_user, pid, daily_limit=int(parts[3]))
        await q.answer(f"Limit {parts[3]}/day")
        await show(*await _profile_card(await arun(db.get_user, pid), active))
    elif kind == "lang" and len(parts) > 3 and parts[3] in LETTER_LANGS:
        await arun(db.update_user, pid, letter_lang=parts[3])
        await q.answer(f"Letters: {LETTER_LANGS[parts[3]]}")
        await show(*await _profile_card(await arun(db.get_user, pid), active))
    elif kind == "del":
        await q.answer()
        await show(f"🗑 Delete profile <b>{esc(db.profile_name(p))}</b> with its CV, attachments, sender, "
                   "settings and application history? Emails already sent cannot be recalled.",
                   InlineKeyboardMarkup([[B("🗑 Yes, delete", callback_data=f"prof:delyes:{pid}"),
                                          B("Cancel", callback_data=f"prof:open:{pid}")]]))
    elif kind == "delyes":
        await q.answer()
        await wipe_user(pid)
        if pid == active:
            await set_active(owner, owner, ctx)
        log.info("[profiles] user %s deleted profile %s", owner, pid)
        await show(*await _profiles_view(owner, owner if pid == active else active))
    else:
        # actions that reuse the normal commands: switch to this profile first, then run the command
        actions = {"auto": (auto_cmd, ["on"]), "run": (run_cmd, []), "search": (search_cmd, []),
                   "sender": (sender_cmd, []), "rep": (replies_cmd, []), "files": (files_cmd, [])}
        if kind not in actions:
            await q.answer()
            return
        if pid != active:
            await set_active(owner, pid, ctx)
            await q.answer(f"⭐ {db.profile_name(p)} is now active")
        else:
            await q.answer()
        fn, args = actions[kind]
        ctx.args = list(args)
        await fn(update, ctx)


# ------------------------------------------------------------------ letter language
def _lang_kb(cur: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B(("✅ " if cur == code else "") + label, callback_data=f"lang:{code}")
                                  for code, label in LETTER_LANGS.items()]])


async def language_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = await current_pid(update, ctx)
    user = await arun(db.get_user, pid)
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in LETTER_LANGS:
        await arun(db.update_user, pid, letter_lang=arg)
        user = {**(user or {}), "letter_lang": arg}
        await update.effective_message.reply_text(f"✅ New cover letters for {db.profile_name(user)}: "
                                                  f"{LETTER_LANGS[arg]}.")
        return
    cur = letter_lang(user)
    await update.effective_message.reply_text(
        f"✍️ Cover letter language for {db.profile_name(user)}: {LETTER_LANGS[cur]}\n\n"
        "Applies to new letters (auto mode and Apply). Letters already queued keep their language.",
        reply_markup=_lang_kb(cur))


async def on_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    code = q.data.split(":", 1)[1]
    if code not in LETTER_LANGS:
        await q.answer()
        return
    pid = await current_pid(update, ctx)
    await arun(db.update_user, pid, letter_lang=code)
    await q.answer(f"Letters: {LETTER_LANGS[code]}")
    user = await arun(db.get_user, pid)
    try:
        await q.edit_message_text(f"✍️ Cover letter language for {db.profile_name(user)}: {LETTER_LANGS[code]}\n\n"
                                  "Applies to new letters (auto mode and Apply). Letters already queued keep "
                                  "their language.", reply_markup=_lang_kb(code))
    except Exception:
        pass


# ------------------------------------------------------------------ stats / admin
async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await arun(db.client_stats, await current_pid(update, ctx))
    if not rows:
        await update.effective_message.reply_text("No data yet.")
        return
    r = rows[0]
    await update.effective_message.reply_text(
        f"📊 Sent today: {r['sent_today']}\nSent total: {r['sent_total']}\nQueued: {r['queued']}\n"
        f"Failed: {r['failed']}\nWebsite applications (marked): {r['web_applied']}\n"
        f"🎉 Interviews: {r.get('interviews') or 0} · ❌ Rejections: {r.get('rejections') or 0} · "
        f"⚠️ Bounced: {r.get('bounced') or 0}")


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
            f"\n   limit {u.get('daily_limit')}/day · "
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
            cu = await arun(db.get_user, cid)
            if db.chat_id(cu):
                await ctx.bot.send_message(db.chat_id(cu), f"⏸ An admin turned auto mode off for {db.profile_name(cu)}.")
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
    uid = await current_pid(update, ctx)
    doc = update.message.document
    if ctx.user_data.get("att_mode"):                  # /files -> Add: documents are attachments, not a CV
        await add_attachment(update, ctx, doc, doc.file_name or "file", doc.file_size)
        return
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
                profile = await asyncio.to_thread(parse_cv_sync, path, USER_WAIT_S)
        with step("upload CV to storage"):
            key = await asyncio.to_thread(storage.upload_cv, uid, data, ext)
    except GeminiBusy:
        log.warning("[cv] Gemini busy, user %s asked to retry", uid)
        await msg.edit_text("⏳ Google's AI (Gemini) is overloaded right now, so I couldn't read the CV. "
                            "Please send it again in a few minutes.")
        return
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
    uid = await current_pid(update, ctx)
    user = await arun(db.get_user, uid)
    profile = user and user.get("profile")
    if not profile:
        await update.effective_message.reply_text("Send your CV first.")
        return
    where = user.get("location")
    titles = profile.get("job_titles") or []
    log.info("[search] user=%s where=%s titles=%s", uid, where or "Germany", titles)
    msg = await update.effective_message.reply_text(
        f"👤 {db.profile_name(user)}\n🔎 Searching jobs in {where or 'all of Germany'}...")
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
        row = [InlineKeyboardButton("✍️ Apply", callback_data=cb("apply", job["refnr"], uid)),
               InlineKeyboardButton("🔗 Open", url=job["url"]),
               InlineKeyboardButton("Skip", callback_data=cb("skip", job["refnr"], uid))]
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup([row]),
                                        disable_web_page_preview=True)
        await asyncio.sleep(0.4)  # stay under Telegram's flood limits
    log.info("[search] showed %d jobs to user %s", min(len(jobs), MAX_SHOW), uid)


# ------------------------------------------------------------------ auto command
async def auto_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = await current_pid(update, ctx)
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
            f"Location: {user.get('location') or 'all of Germany'}\n\n"
            f"/auto on | off | limit N (1-{MAX_DAILY})")
        return

    if sub == "on":
        if not user.get("profile") or not user.get("cv_path"):
            await update.effective_message.reply_text("Send your CV first.")
            return
        if not sender_ok(user):
            await update.effective_message.reply_text("Connect your email first with /sender.")
            return
        if not user.get("consent_at"):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I agree", callback_data=cb("autoconsent", "yes", uid)),
                                        InlineKeyboardButton("No", callback_data="autoconsent:no")]])
            await update.effective_message.reply_text(
                "Auto mode applies to jobs for you without asking each time.\n\n"
                "• Your CV, cover letter (also as PDF) and /files attachments are emailed to employers "
                "from your email address.\n"
                "• Your CV is processed by Google's Gemini API.\n"
                f"• You get a Telegram message with a Cancel button; mails wait at least {CANCEL_WINDOW_MIN} minutes "
                f"and go out only Mon-Fri {WORK_START}:00-{WORK_END}:00 (Berlin).\n"
                "• Letters are AI-written and not reviewed by a human before sending. They may contain mistakes.\n"
                "• /delete removes all data of this profile at any time.\n\n"
                "Do you agree?", reply_markup=kb)
            return
        await arun(db.update_user, uid, auto_on=True)
        await update.effective_message.reply_text("🤖 Auto mode ON." + (" (DRY RUN: mails go to yourself)" if DRY_RUN else ""))
    elif sub == "off":
        await arun(db.update_user, uid, auto_on=False)
        await update.effective_message.reply_text("Auto mode OFF. Already queued letters can still be cancelled with their button.")
    elif sub == "limit" and len(ctx.args) > 1 and ctx.args[1].isdigit():
        n = int(ctx.args[1])
        if not 1 <= n <= MAX_DAILY:
            await update.effective_message.reply_text(f"Limit must be between 1 and {MAX_DAILY}.")
            return
        await arun(db.update_user, uid, daily_limit=n)
        await update.effective_message.reply_text(f"Saved: limit = {n}/day")
    else:
        await update.effective_message.reply_text("Usage: /auto on | off | limit N | status")


# ------------------------------------------------------------------ delete
async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = await current_pid(update, ctx)
    name = db.profile_name(await arun(db.get_user, pid))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Yes, delete everything", callback_data=cb("delete", "yes", pid)),
                                InlineKeyboardButton("Cancel", callback_data="delete:no")]])
    await update.effective_message.reply_text(
        f"Profile: {name}\n"
        "This permanently deletes this profile's CV, attachments, email connection, settings, drafts and "
        "application history. "
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
async def make_draft(q, job: dict, uid: int):
    user = await arun(db.get_user, uid)
    if not user or not user.get("profile"):
        await q.message.reply_text("Send your CV first.")
        return
    note = await q.message.reply_text("✍️ Writing your cover letter...")
    try:
        with step(f"write cover letter {job['refnr']}"):
            letter = await asyncio.to_thread(write_letter_sync, user["profile"], job, user.get("note"),
                                             USER_WAIT_S, letter_lang(user))
    except GeminiBusy:
        await note.edit_text("⏳ Google's AI (Gemini) is overloaded right now. Press Apply again in a few minutes.")
        return
    except Exception as e:
        log.exception("letter failed")
        await note.edit_text(f"Could not write the letter: {e}")
        return
    await arun(db.upsert_app, uid, job, status="draft", mode="manual", letter=letter, error=None)
    ref = job["refnr"]
    preview = (f"👤 {esc(db.profile_name(user))} · {LETTER_LANGS[letter['lang']]} letter\n"
               f"<b>{esc(letter['subject'])}</b>\n\n{esc(letter['body'])}")
    if job.get("email"):
        kb = [[InlineKeyboardButton("✉️ Send to employer", callback_data=cb("send", ref, uid)),
               InlineKeyboardButton("🔄 Rewrite", callback_data=cb("regen", ref, uid)),
               InlineKeyboardButton("✖ Cancel", callback_data=cb("cancel", ref, uid))]]
        await note.edit_text(preview, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    else:
        kb = [[InlineKeyboardButton("🔗 Open application page", url=job["url"])],
              [InlineKeyboardButton("🔄 Rewrite", callback_data=cb("regen", ref, uid)),
               InlineKeyboardButton("✅ Mark as applied", callback_data=cb("done", ref, uid))]]
        await note.edit_text(preview + "\n\n<i>This job uses a website form. Copy the letter, "
                             "or upload the PDF / DOCX below, and apply on the page.</i>",
                             parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        name = letters.safe_name(user["profile"].get("full_name") or "")
        try:
            pdf = await asyncio.to_thread(letters.letter_pdf, user["profile"], job, letter)
            await q.message.reply_document(pdf, filename=f"{letters.file_word(letter, 'letter')}_{name}.pdf")
        except Exception:
            log.exception("[letters] PDF failed for %s", ref)
        path = await asyncio.to_thread(letter_docx, uid, job, letter)
        try:
            with open(path, "rb") as fh:
                await q.message.reply_document(fh, filename=f"{letters.file_word(letter, 'letter')}_{name}.docx")
        finally:
            path.unlink(missing_ok=True)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, arg = q.data.split(":", 1)
    arg, _, pid_s = arg.partition("@")
    uid = await current_pid(update, ctx)
    if pid_s.lstrip("-").isdigit():                   # the button belongs to a specific profile
        uid = int(pid_s)
        if not await owned_profile(q.from_user.id, uid):
            await q.message.reply_text("That profile no longer exists.")
            return
    log.info("[tg] button %s %s from user %s (profile %s)", action, arg, q.from_user.id, uid)

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
            name = db.profile_name(await arun(db.get_user, uid))
            await wipe_user(uid)
            if uid != q.from_user.id:                 # an extra profile: switch back to the owner's own
                await set_active(q.from_user.id, q.from_user.id, ctx)
            await q.message.reply_text(f"🗑 Profile {name} and all its data were deleted.")
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
        await make_draft(q, job, uid)

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
                files = await asyncio.to_thread(mailer.application_files, user, job, app["letter"])
                sent_to, msg_id = await asyncio.to_thread(mailer.send, user, job, app["letter"], files)
        except Exception as e:
            log.exception("send failed")
            await arun(db.set_status, uid, refnr, "draft", ["sending"], error=str(e)[:500])
            await q.message.reply_text(f"Sending failed: {e}")
            return
        await arun(db.set_status, uid, refnr, "sent", ["sending"],
                   sent_at=now_iso(), error=None, to_email=job["email"], message_id=msg_id)
        await clear_kb(q)
        note = " (DRY RUN: sent to yourself)" if DRY_RUN else ""
        await q.message.reply_text(f"✅ Application sent to {esc(sent_to)}{note}\n{job_line(job, app['letter'])}",
                                   parse_mode="HTML", disable_web_page_preview=True)