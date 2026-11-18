"""Auto mode: scheduled scan (find -> filter -> letter -> queue), send worker, admin digest."""
import asyncio, html, random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import sheets
import db, mailer, storage
from db import arun
from jobs import find_jobs_cached, is_bad_email
from llm import write_letter_sync
from matching import quick_info
from config import (TZ, WORK_START, WORK_END, CANCEL_WINDOW_MIN, JITTER_MAX_MIN, SEND_BATCH,
                    SEND_GAP, EMPLOYER_COOLDOWN_DAYS, DRY_RUN, ALLOW_SHARED_SENDER, ADMIN_IDS, log)

esc = lambda x: html.escape(str(x if x is not None else ""))


def in_work_window() -> bool:
    now = datetime.now(TZ)
    return now.weekday() < 5 and WORK_START <= now.hour < WORK_END


async def notify_admins(ctx, text: str):
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            log.warning("[admin] could not notify %s", aid)


# ------------------------------------------------------------------ scan
async def scan_all(ctx: ContextTypes.DEFAULT_TYPE):
    users = await arun(db.list_auto_users)
    log.info("[auto] scan_all: %d user(s) with auto mode", len(users))
    for u in users:
        try:
            await scan_user(ctx, u)
        except Exception:
            log.exception("[auto] scan failed for user %s", u["tg_id"])


async def scan_user(ctx: ContextTypes.DEFAULT_TYPE, u: dict, immediate: bool = False) -> dict:
    uid, profile = u["tg_id"], u["profile"]
    st = dict(total=0, known=0, no_email=0, german=0, low_score=0,
              cooldown=0, letter_failed=0, queued=0, note="")
    if not (db.has_sender(u) or DRY_RUN or ALLOW_SHARED_SENDER):
        st["note"] = "no sender connected"
        return st
    remaining = u["daily_limit"] - await arun(db.count_auto_today, uid)
    if remaining <= 0:
        st["note"] = "daily limit reached"
        return st

    jobs = await find_jobs_cached(profile.get("job_titles") or [], u.get("location"))
    known = set(await arun(db.known_refnrs, uid))
    st["total"] = len(jobs)
    cands = []
    for j in jobs:
        if j["refnr"] in known:
            st["known"] += 1
            continue
        if not j.get("email") or is_bad_email(j["email"]):
            st["no_email"] += 1
            continue
        quick_info(profile, j)
        if j["german_warning"]:
            st["german"] += 1
            continue
        if j["score"] < u["min_score"]:
            st["low_score"] += 1
            continue
        cands.append(j)
    cands.sort(key=lambda j: j["score"], reverse=True)
    log.info("[auto] user %s funnel: %s (min_score=%s)", uid, st, u["min_score"])

    for job in cands:
        if remaining <= 0:
            break
        if await arun(db.employer_recent, uid, db.employer_key(job["employer"]), EMPLOYER_COOLDOWN_DAYS):
            st["cooldown"] += 1
            continue
        try:
            letter = await asyncio.to_thread(write_letter_sync, profile, job, u.get("note"))
        except Exception:
            log.exception("[auto] letter failed for %s", job["refnr"])
            st["letter_failed"] += 1
            continue
        delay = timedelta(0) if immediate else timedelta(
            minutes=CANCEL_WINDOW_MIN + random.uniform(0, JITTER_MAX_MIN))
        send_after = datetime.now(timezone.utc) + delay
        row = await arun(db.insert_queued, uid, job, letter, send_after)
        if not row:
            continue
        remaining -= 1
        st["queued"] += 1
        sheets.fire(uid, job["refnr"])
        await notify_queued(ctx, uid, job, letter, send_after)
    return st

async def notify_queued(ctx, uid, job, letter, send_after):
    when = send_after.astimezone(TZ).strftime("%H:%M")
    text = (f"🤖 <b>Auto-apply queued</b>\n{esc(job['title'])} · {esc(job['employer'])}\n"
            f"Not before {when} (Berlin). Sends only Mon-Fri {WORK_START}:00-{WORK_END}:00.\n\n"
            f"<b>{esc(letter['subject'])}</b>\n\n{esc(letter['body'])}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{job['refnr']}")]])
    try:
        await ctx.bot.send_message(uid, text[:4000], parse_mode="HTML", reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception:
        log.exception("[auto] could not notify user %s", uid)

import smtplib

RETRY_DELAY_MIN = 15
MAX_RETRIES = 6                      # ~1.5 h of outage before giving up
_retries: dict = {}                  # app_id -> attempts (in memory, resets on restart; fine)


def is_transient(e: Exception) -> bool:
    """Network down, DNS, timeout, dropped connection. NOT auth errors or refused recipients."""
    if isinstance(e, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError)):
        return True
    return isinstance(e, OSError) and not isinstance(e, smtplib.SMTPException)


async def requeue(row: dict, err) -> bool:
    when = datetime.now(timezone.utc) + timedelta(minutes=RETRY_DELAY_MIN + random.uniform(0, 5))
    return await arun(db.set_status, row["tg_id"], row["refnr"], "queued", ["sending"],
                      error=str(err)[:500], send_after=when.isoformat())


async def send_due(ctx: ContextTypes.DEFAULT_TYPE, force: bool = False):
    if not force and not in_work_window():
        return
    rows = await arun(db.claim_due, SEND_BATCH)
    for i, row in enumerate(rows):
        if i:
            await asyncio.sleep(random.uniform(*SEND_GAP))
        if await send_one(ctx, row) == "transient":
            # network is down: put the rest of the claimed batch back instead of failing them
            for rest in rows[i + 1:]:
                await requeue(rest, "network unavailable, batch postponed")
            break


async def send_one(ctx, row: dict) -> str:
    """Returns 'ok', 'transient' or 'failed'."""
    uid, refnr, app_id = row["tg_id"], row["refnr"], row["id"]
    who = "?"
    delivered = False
    try:
        user = await arun(db.get_user, uid)
        if not user or not user.get("profile") or not user.get("cv_path"):
            raise RuntimeError("user, profile or CV missing")
        who = (user["profile"].get("full_name") or str(uid))
        cv = await asyncio.to_thread(storage.download_cv, user["cv_path"])
        job = (await arun(db.get_job, refnr)) or {"refnr": refnr, "title": row["title"],
                                                  "employer": row["employer"]}
        job["email"] = row["to_email"]
        sent_to = await asyncio.to_thread(mailer.send, user, job, row["letter"], cv,
                                          Path(user["cv_path"]).suffix)
        delivered = True                                     # mail is out; never retry after this point
        await arun(db.mark_sent, app_id, row["to_email"])
        sheets.fire(uid, refnr)
        _retries.pop(app_id, None)
        extra = " (DRY RUN: sent to yourself)" if DRY_RUN else ""
        msg = f"✅ Auto-sent: {esc(row['title'])} · {esc(row['employer'])}\n→ {esc(sent_to)}{extra}"
        status = "ok"
    except Exception as e:
        n = _retries.get(app_id, 0) + 1
        if not delivered and is_transient(e) and n <= MAX_RETRIES:
            _retries[app_id] = n
            log.warning("[auto] transient error for application %s (attempt %d/%d): %r -> requeued",
                        app_id, n, MAX_RETRIES, e)
            if await requeue(row, e):
                return "transient"                           # silent: no user/admin spam
        log.exception("[auto] send failed for application %s", app_id)
        _retries.pop(app_id, None)
        if delivered:   # mail went out but DB update failed: don't mark failed (would invite a resend)
            msg = f"✅ Auto-sent: {esc(row['title'])} · {esc(row['employer'])}"
            status = "ok"
        else:
            await arun(db.mark_failed, app_id, str(e))
            msg = f"⚠️ Auto-send failed: {esc(row['title'])} · {esc(row['employer'])}\n{esc(str(e)[:200])}"
            await notify_admins(ctx, f"⚠️ <b>Send failed</b> for {esc(who)} ({uid})\n"
                                     f"{esc(row['title'])} · {esc(row['employer'])}\n{esc(str(e)[:300])}")
            status = "failed"
    try:
        await ctx.bot.send_message(uid, msg, parse_mode="HTML")
    except Exception:
        log.warning("[auto] could not notify user %s", uid)
    return status

# ------------------------------------------------------------------ admin digest
def fmt_stats_line(r: dict) -> str:
    last = str(r["last_sent"])[:16].replace("T", " ") if r.get("last_sent") else "-"
    return (f"👤 <b>{esc(r.get('name') or r['tg_id'])}</b> ({r['tg_id']}) · auto {'ON' if r['auto_on'] else 'off'}\n"
            f"   sender: {esc(r.get('sender') or 'none')}\n"
            f"   today {r['sent_today']} · total {r['sent_total']} · queued {r['queued']} · "
            f"failed {r['failed']} · web {r['web_applied']} · last {last}")


async def daily_digest(ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_IDS:
        return
    rows = await arun(db.client_stats)
    if not rows:
        return
    total = sum(r["sent_today"] for r in rows)
    body = "\n\n".join(fmt_stats_line(r) for r in rows)
    await notify_admins(ctx, f"📊 <b>Daily summary</b>: {total} application(s) sent today\n\n{body}"[:4000])