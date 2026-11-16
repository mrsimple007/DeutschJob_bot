"""One master Google Sheet, one tab per sender email, one row per application.
Failures here must never break sending, so every entry point catches and logs."""
import asyncio, threading
from datetime import datetime, timezone
from pathlib import Path

import gspread

import db
from config import GOOGLE_SA_FILE, MASTER_SHEET_ID, TZ, log

BASE = Path(__file__).resolve().parent
HEADERS = ["Applied (Berlin)", "Job title", "Employer", "Email", "Job link",
           "Status", "Mode", "Outcome", "Notes", "Ref"]
OUTCOMES = ["Waiting", "Interview", "Approved", "Rejected", "No response"]
COLORS = {                      # RGB 0-1
    "Waiting":     (1.00, 0.95, 0.60),   # yellow
    "Interview":   (0.72, 0.88, 0.72),   # green
    "Approved":    (0.45, 0.78, 0.50),   # stronger green
    "Rejected":    (0.96, 0.68, 0.68),   # red
    "No response": (0.85, 0.85, 0.85),   # grey
}
REF_COL = len(HEADERS)            # column J, hidden, used to find the row
LABEL = {"queued": "Queued", "sending": "Sending", "sent": "Sent", "failed": "Failed",
         "cancelled": "Cancelled", "applied_manual": "Applied (website)"}

_lock = threading.RLock()
_gc = None
_sh = None
_tabs = {}          # tab title -> Worksheet
_tasks = set()


def _sa_path() -> Path:
    p = Path(GOOGLE_SA_FILE or "")
    return p if p.is_absolute() else BASE / p


def enabled() -> bool:
    return bool(GOOGLE_SA_FILE) and bool(MASTER_SHEET_ID) and _sa_path().is_file()


def url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}"


def _client():
    global _gc
    if _gc is None:
        _gc = gspread.service_account(filename=str(_sa_path()))
    return _gc


def _master():
    global _sh
    if _sh is None:
        _sh = _client().open_by_key(MASTER_SHEET_ID)
    return _sh


def _safe(v) -> str:
    """Stop formula injection: job data comes from the internet."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _when(app: dict) -> str:
    s = app.get("sent_at") or app.get("created_at")
    if not s:
        return ""
    try:
        dt = datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(s)[:16]


def _values(app: dict, job) -> list:
    return [_safe(x) for x in (_when(app), app.get("title"), app.get("employer"),
                               app.get("to_email"), (job or {}).get("url"),
                               LABEL.get(app["status"], app["status"]), app.get("mode"))]


# ------------------------------------------------------------------ tabs
def apply_colors(sh, ws) -> None:
    """Colors the Outcome column (H) of one tab. Replaces only that tab's own rules."""
    existing = 0
    for s in sh.fetch_sheet_metadata()["sheets"]:
        if s["properties"]["sheetId"] == ws.id:
            existing = len(s.get("conditionalFormats", []))
    reqs = [{"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}} for _ in range(existing)]
    for i, (label, (r, g, b)) in enumerate(COLORS.items()):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [{"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 7, "endColumnIndex": 8}],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": label}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}})
    sh.batch_update({"requests": reqs})


def _format_new_tab(sh, ws) -> None:
    ws.append_row(HEADERS)
    ws.freeze(rows=1)
    ws.format("A1:J1", {"textFormat": {"bold": True}})
    ws.format("J:J", {"numberFormat": {"type": "TEXT"}})
    sh.batch_update({"requests": [
        {"setDataValidation": {   # dropdown for the Outcome column (H)
            "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 7, "endColumnIndex": 8},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": o} for o in OUTCOMES]},
                     "showCustomUi": True, "strict": False}}},
        {"updateDimensionProperties": {   # hide the Ref column (J)
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 9, "endIndex": 10},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ]})
    apply_colors(sh, ws)


def _ensure_tab(email: str):
    """Returns the tab for this sender email, creating it if needed.
    One-time migration: the old single 'Applications' tab is renamed to the first email."""
    title = (email or "shared")[:100]
    if title in _tabs:
        return _tabs[title]
    sh = _master()
    by_title = {w.title: w for w in sh.worksheets()}
    if title in by_title:
        ws = by_title[title]
    elif "Applications" in by_title:
        ws = by_title["Applications"]
        ws.update_title(title)
        try:
            apply_colors(sh, ws)
        except Exception:
            log.warning("[sheets] could not apply colors to %s", title, exc_info=True)
    else:
        ws = sh.add_worksheet(title, rows=200, cols=len(HEADERS))
        _format_new_tab(sh, ws)
    _tabs[title] = ws
    return ws


def _upsert(ws, items: list) -> None:
    """items = [(refnr, 7 system values)]. Updates columns A-G of existing rows only,
    so the client's Outcome and Notes are never touched. New rows get Outcome=Waiting."""
    refs = ws.col_values(REF_COL)
    index = {r: i + 1 for i, r in enumerate(refs) if r}
    updates, new_rows = [], []
    for refnr, vals in items:
        if refnr in index:
            r = index[refnr]
            updates.append({"range": f"A{r}:G{r}", "values": [vals]})
        else:
            new_rows.append(vals + ["Waiting", "", refnr])
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")


def _stamp(app: dict, user: dict) -> str:
    """Which mailbox (= tab) an application belongs to. Set once, then never changes."""
    if app.get("sender_email"):
        return app["sender_email"]
    sender = user.get("smtp_user") or "shared"
    db.set_app_sender(app["id"], sender)
    app["sender_email"] = sender
    return sender


# ------------------------------------------------------------------ sharing
def share(email: str) -> None:
    _master().share(email, perm_type="user", role="writer", notify=True)


# ------------------------------------------------------------------ sync
def _sync_app(uid: int, refnr: str) -> None:
    if not enabled():
        return
    user = db.get_user(uid)
    app = db.get_app(uid, refnr)
    if not user or not app or app["status"] in ("draft", "skipped"):
        return
    job = db.get_job(refnr)
    with _lock:
        try:
            ws = _ensure_tab(_stamp(app, user))
            _upsert(ws, [(refnr, _values(app, job))])
        except Exception:
            _tabs.clear()          # tab may have been renamed or deleted: look it up again next time
            raise


def sync_user(uid: int) -> int:
    """Blocking. Rebuilds/repairs every row of one user in the right tabs. Returns the number of applications."""
    user = db.get_user(uid)
    if not enabled() or not user:
        return 0
    groups = {}
    for a in db.all_apps(uid):
        groups.setdefault(_stamp(a, user), []).append((a["refnr"], _values(a, db.get_job(a["refnr"]))))
    with _lock:
        try:
            for sender, items in groups.items():
                ws = _ensure_tab(sender)
                _upsert(ws, items)
                try:
                    apply_colors(_master(), ws)
                except Exception:
                    log.warning("[sheets] could not apply colors to %s", sender, exc_info=True)
        except Exception:
            _tabs.clear()
            raise
    return sum(len(v) for v in groups.values())


def remove_user_rows(uid: int) -> None:
    """Blocking. Deletes this user's rows from the master sheet (used by /delete)."""
    if not enabled():
        return
    groups = {}
    for a in db.all_apps(uid):
        if a.get("sender_email"):
            groups.setdefault(a["sender_email"], set()).add(a["refnr"])
    with _lock:
        sh = _master()
        for sender, refs in groups.items():
            ws = _ensure_tab(sender)
            col = ws.col_values(REF_COL)
            rows = sorted((i + 1 for i, r in enumerate(col) if r in refs), reverse=True)
            if rows:
                sh.batch_update({"requests": [
                    {"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                                   "startIndex": r - 1, "endIndex": r}}}
                    for r in rows]})


async def sync_app(uid: int, refnr: str) -> None:
    try:
        await asyncio.to_thread(_sync_app, uid, refnr)
    except Exception:
        log.warning("[sheets] sync failed for user %s job %s", uid, refnr, exc_info=True)


def fire(uid: int, refnr: str) -> None:
    """Fire-and-forget from async code, so Telegram replies and sending never wait for Google."""
    if not enabled():
        return
    t = asyncio.get_running_loop().create_task(sync_app(uid, refnr))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


log.info("[sheets] enabled=%s file=%s master=%s", enabled(), _sa_path(), MASTER_SHEET_ID or "-")