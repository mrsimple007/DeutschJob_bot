"""Reply tracking: read the client's inbox over IMAP (read-only), match employer replies and bounces
to sent applications, classify replies with Gemini, then update DB + sheet and notify the client.

Privacy: only headers (From, Subject, Date, IDs) of recent mails are scanned. A message body is
downloaded only when the headers match one of the client's applications. Nothing in the mailbox
is changed: the folder is opened read-only and BODY.PEEK never sets the \\Seen flag."""
import asyncio, email, html, imaplib, re, ssl
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

import crypto, db, sheets
from db import arun, now_iso
from jobs import clean_html
from llm import classify_reply_sync
from config import REPLY_LOOKBACK_DAYS, REPLY_MAX_SCAN, log

esc = lambda x: html.escape(str(x if x is not None else ""))

# smtp host -> imap host
IMAP_HOSTS = {
    "smtp.gmail.com": "imap.gmail.com",
    "mail.gmx.net": "imap.gmx.net",
    "smtp.web.de": "imap.web.de",
    "securesmtp.t-online.de": "secureimap.t-online.de",
    "smtp.mail.yahoo.com": "imap.mail.yahoo.com",
    "smtp.mail.me.com": "imap.mail.me.com",
    "smtp-mail.outlook.com": "outlook.office365.com",
}
# replies from these domains are only matched by exact address, never by domain
FREEMAIL = {"gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.com", "web.de", "t-online.de",
            "yahoo.com", "yahoo.de", "outlook.com", "outlook.de", "hotmail.com", "hotmail.de",
            "live.com", "icloud.com", "me.com", "aol.com", "freenet.de", "posteo.de", "mail.de"}
BOUNCE_LOCAL = ("mailer-daemon", "postmaster")
BOUNCE_SUBJ = re.compile(r"undeliver|delivery status notification|delivery failure|returned mail|"
                         r"failure notice|unzustellbar|nicht zugestellt|zustellung fehlgeschlagen", re.I)
QUOTE_CUT = re.compile(r"\n\s*(?:-{2,}\s*(?:Original|Ursprüngliche)|Am .{5,120}schrieb|"
                       r"On .{5,120}wrote:|Von:\s|From:\s)", re.I)
HEADER_FIELDS = "FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES DATE"
MAX_BODIES = 50                   # bodies downloaded per check at most
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

CATEGORY_OUTCOME = {"interview": "interview", "rejection": "rejected",
                    "acknowledgement": "acknowledged", "other": "other"}
SHEET_LABEL = {"interview": "Interview", "rejected": "Rejected", "bounced": "Bounced"}
FINAL = ("interview", "rejected", "bounced")
NOTIFY = {
    "interview": "🎉 <b>Interview invitation!</b>",
    "rejected": "❌ <b>Rejection</b>",
    "acknowledged": "📨 <b>Receipt confirmed</b>",
    "other": "✉️ <b>Employer replied</b>",
    "bounced": "⚠️ <b>Not delivered</b> (address doesn't exist). Auto mode won't use it again.",
}


# ------------------------------------------------------------------ helpers
def imap_host_for(u: dict) -> str:
    if u.get("imap_host"):
        return u["imap_host"]
    smtp = (u.get("smtp_host") or "").lower()
    return IMAP_HOSTS.get(smtp) or re.sub(r"^(smtp|mail|securesmtp|smtps)\.", "imap.", smtp)


def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return str(v or "")


def _ids(v) -> list:
    return re.findall(r"<[^<>\s]+>", v or "")


def _addr(v) -> str:
    return parseaddr(_dec(v))[1].lower()


def _domain(a: str) -> str:
    return a.rsplit("@", 1)[-1] if "@" in (a or "") else ""


def _date_iso(v) -> Optional[str]:
    try:
        return parsedate_to_datetime(v).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _connect(host: str, user: str, password: str) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=30)
    m.login(user, password)
    return m


def verify_imap(host: str, user: str, password: str) -> None:
    """Blocking. Raises if the IMAP login fails."""
    m = _connect(host, user, password)
    try:
        m.logout()
    except Exception:
        pass


def match_app(hdr, apps: list):
    """Returns (application or None, 'reply' | 'bounce' | None) from headers only."""
    frm = _addr(hdr.get("From"))
    subj = _dec(hdr.get("Subject"))
    if frm.split("@")[0].startswith(BOUNCE_LOCAL) or BOUNCE_SUBJ.search(subj):
        return None, "bounce"                      # the application is found later, in the body
    refs = set(_ids(hdr.get("In-Reply-To"))) | set(_ids(hdr.get("References")))
    for a in apps:                                 # 1. threading headers (most reliable)
        if a.get("message_id") and a["message_id"] in refs:
            return a, "reply"
    for a in apps:                                 # 2. our subject carries "(Referenznummer <refnr>)"
        if a.get("refnr") and a["refnr"] in subj:
            return a, "reply"
    for a in apps:                                 # 3. same address we wrote to
        if a.get("to_email") and frm == a["to_email"].lower():
            return a, "reply"
    dom = _domain(frm)                             # 4. same company domain (not freemail)
    if dom and dom not in FREEMAIL:
        same = [a for a in apps if _domain((a.get("to_email") or "").lower()) == dom]
        if same:
            return max(same, key=lambda a: a.get("sent_at") or ""), "reply"
    return None, None


def _body_text(msg) -> str:
    plain, htm = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        try:
            txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            txt = payload.decode("utf-8", errors="replace")
        if ct == "text/plain" and not plain:
            plain = txt
        elif ct == "text/html" and not htm:
            htm = txt
    text = plain or clean_html(htm)
    text = QUOTE_CUT.split(text, maxsplit=1)[0]                    # drop the quoted application
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return text.strip()[:4000]


def _bounce_app(raw: str, apps: list):
    low = raw.lower()
    for a in apps:
        if a.get("message_id") and a["message_id"] in raw:
            return a
    for a in apps:
        if a.get("to_email") and a["to_email"].lower() in low:
            return a
    return None


# ------------------------------------------------------------------ IMAP fetch (blocking)
def fetch_sync(u: dict, apps: list, since: date, seen: set) -> list:
    """Returns new matched messages: dicts with message_id, app, kind ('reply'|'bounce'|'ignored'),
    from, subject, date, text. 'ignored' = bounce we could not link; recorded so it is not re-read."""
    host, me = imap_host_for(u), u["smtp_user"].lower()
    m = _connect(host, u["smtp_user"], crypto.decrypt(u["smtp_pass_enc"]))
    out = []
    try:
        m.select("INBOX", readonly=True)
        _, data = m.uid("search", None, "SINCE", _imap_date(since))
        uids = (data[0] or b"").split()[-REPLY_MAX_SCAN:]
        cands = []
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i:i + 100]).decode()
            _, data = m.uid("fetch", chunk, f"(UID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                um = re.search(rb"UID (\d+)", item[0])
                if not um:
                    continue
                uid = um.group(1).decode()
                hdr = email.message_from_bytes(item[1])
                mid = (_ids(hdr.get("Message-ID")) or [f"<imap-uid-{uid}@{host}>"])[0]
                if mid in seen or _addr(hdr.get("From")) == me:
                    continue
                app, kind = match_app(hdr, apps)
                if kind:
                    cands.append((uid, mid, hdr, app, kind))
        for uid, mid, hdr, app, kind in cands[-MAX_BODIES:]:
            _, data = m.uid("fetch", uid, "(BODY.PEEK[]<0.300000>)")
            raw = next((x[1] for x in data or [] if isinstance(x, tuple)), b"")
            item = {"message_id": mid, "app": app, "kind": kind, "from": _addr(hdr.get("From")),
                    "subject": _dec(hdr.get("Subject"))[:200], "date": _date_iso(hdr.get("Date")),
                    "text": ""}
            if kind == "bounce":
                item["app"] = _bounce_app(raw.decode("utf-8", errors="replace"), apps)
                if not item["app"]:
                    item["kind"] = "ignored"
            else:
                item["text"] = _body_text(email.message_from_bytes(raw))
            out.append(item)
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ processing (async)
def _should_set(cur: Optional[str], new: str) -> bool:
    """Weak outcomes (acknowledged/other) never overwrite a final one."""
    return new in FINAL or cur not in FINAL


async def _notify(ctx, u: dict, app: dict, outcome: str, summary: str, frm: str):
    uid = u["tg_id"]
    text = (f"👤 <b>{esc(db.profile_name(u))}</b>\n{NOTIFY[outcome]}\n{esc(app.get('title'))} · {esc(app.get('employer'))}"
            + (f"\n<i>{esc(summary)}</i>" if summary and outcome != "bounced" else "")
            + (f"\nFrom: {esc(frm)}" if frm and outcome != "bounced" else f"\nTo: {esc(app.get('to_email'))}"))
    try:
        await ctx.bot.send_message(db.chat_id(u), text[:4000], parse_mode="HTML",
                                   disable_notification=outcome == "acknowledged")
    except Exception:
        log.warning("[replies] could not notify user %s", uid)


async def check_user(ctx, u: dict, notify_login_error: bool = True) -> dict:
    uid = u["tg_id"]
    st = dict(apps=0, new=0, interview=0, rejected=0, acknowledged=0, other=0, bounced=0, error="")
    cutoff = datetime.now(timezone.utc) - timedelta(days=REPLY_LOOKBACK_DAYS)
    apps = await arun(db.sent_apps_since, uid, cutoff.isoformat())
    st["apps"] = len(apps)
    if not apps:
        return st
    since = min(date.fromisoformat(a["sent_at"][:10]) for a in apps if a.get("sent_at"))
    if u.get("imap_checked_at"):
        since = max(since, date.fromisoformat(u["imap_checked_at"][:10]) - timedelta(days=2))
    seen = set(await arun(db.seen_message_ids, uid))

    try:
        items = await asyncio.to_thread(fetch_sync, u, apps, since, seen)
    except imaplib.IMAP4.error as e:              # login rejected / IMAP disabled: tell the user once
        err = f"{type(e).__name__}: {e}"[:200]
        log.warning("[replies] IMAP error for user %s: %s", uid, err)
        if notify_login_error and not u.get("imap_error"):
            try:
                await ctx.bot.send_message(db.chat_id(u), f"👤 {db.profile_name(u)}\n"
                                           "📬 Reply tracking could not log in to the mailbox "
                                           f"({imap_host_for(u)}). Check /replies.")
            except Exception:
                pass
        await arun(db.update_user, uid, imap_error=err)
        st["error"] = err
        return st
    except (OSError, ssl.SSLError) as e:          # network: try again next round, no user message
        log.warning("[replies] network error for user %s: %r", uid, e)
        st["error"] = f"network: {type(e).__name__}"
        return st

    for it in items:
        app = it["app"]
        if it["kind"] == "ignored" or not app:
            await arun(db.add_reply, uid, it["message_id"], None, "ignored", it["date"])
            continue
        if it["kind"] == "bounce":
            outcome, summary = "bounced", "Delivery failed: address does not exist or rejected the mail."
        else:
            try:
                cls = await asyncio.to_thread(classify_reply_sync, app, it["subject"], it["text"])
            except Exception:
                log.exception("[replies] classification failed for user %s app %s", uid, app["id"])
                continue                          # not recorded -> retried next round
            outcome, summary = CATEGORY_OUTCOME[cls["category"]], cls["summary"]
        if not await arun(db.add_reply, uid, it["message_id"], app["id"], outcome, it["date"]):
            continue                              # another check processed it meanwhile
        if outcome == "bounced" and app.get("to_email"):
            await arun(db.add_bad_email, app["to_email"], f"bounce for {app['refnr']}")
        if _should_set(app.get("outcome"), outcome):
            await arun(db.set_outcome, app["id"], outcome, summary, it["date"])
            app["outcome"] = outcome
            sheets.fire_outcome(uid, app["refnr"], SHEET_LABEL.get(outcome), summary)
        st["new"] += 1
        st[outcome] += 1
        log.info("[replies] user %s app %s -> %s", uid, app["id"], outcome)
        await _notify(ctx, u, app, outcome, summary, it["from"])

    await arun(db.update_user, uid, imap_checked_at=now_iso(), imap_error=None)
    return st


async def check_all(ctx):
    users = await arun(db.list_reply_users)
    log.info("[replies] checking %d inbox(es)", len(users))
    for u in users:
        try:
            await check_user(ctx, u)
        except Exception:
            log.exception("[replies] check failed for user %s", u["tg_id"])
