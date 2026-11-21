"""Supabase data layer. All functions are synchronous; call them from async code via `arun`."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, TZ

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

T_USERS = "deutschjob_users"
T_JOBS = "deutschjob_jobs"
T_CACHE = "deutschjob_search_cache"
T_APPS = "deutschjob_applications"

ACTIVE = ("queued", "sending", "sent", "applied_manual")   # counts as "applied"


async def arun(fn, *args, **kwargs):
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def employer_key(name: Optional[str]) -> str:
    return re.sub(r"\W+", " ", (name or "").lower()).strip()

def all_apps(tg_id: int) -> list:
    r = (sb.table(T_APPS).select("*").eq("tg_id", tg_id)
         .not_.in_("status", ["draft", "skipped"]).order("created_at").limit(5000).execute())
    return r.data

# ------------------------------------------------------------------ users
def get_user(tg_id: int) -> Optional[dict]:
    r = sb.table(T_USERS).select("*").eq("tg_id", tg_id).limit(1).execute()
    return r.data[0] if r.data else None


def update_user(tg_id: int, **fields) -> None:
    """Create the user if needed, then set the given fields."""
    row = {"tg_id": tg_id, "updated_at": now_iso(), **fields}
    sb.table(T_USERS).upsert(row, on_conflict="tg_id").execute()


def delete_user(tg_id: int) -> None:
    """Deletes the user; applications go with it (ON DELETE CASCADE)."""
    sb.table(T_USERS).delete().eq("tg_id", tg_id).execute()


def list_auto_users() -> list:
    r = sb.table(T_USERS).select("*").eq("auto_on", True).execute()
    return [u for u in r.data if u.get("consent_at") and u.get("profile") and u.get("cv_path")]


# ------------------------------------------------------------------ jobs + search cache
_TRANSIENT = ("score", "matched_skills", "german_warning")


def upsert_jobs(jobs: list) -> None:
    if not jobs:
        return
    rows = [{"refnr": j["refnr"],
             "data": {k: v for k, v in j.items() if k not in _TRANSIENT},
             "fetched_at": now_iso()} for j in jobs]
    sb.table(T_JOBS).upsert(rows, on_conflict="refnr").execute()


def get_job(refnr: str) -> Optional[dict]:
    r = sb.table(T_JOBS).select("data").eq("refnr", refnr).limit(1).execute()
    return r.data[0]["data"] if r.data else None


def get_cache(k: str, max_age_h: float) -> Optional[list]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_h)).isoformat()
    r = sb.table(T_CACHE).select("data").eq("k", k).gte("ts", cutoff).limit(1).execute()
    return r.data[0]["data"] if r.data else None


def put_cache(k: str, data: list) -> None:
    sb.table(T_CACHE).upsert({"k": k, "data": data, "ts": now_iso()}, on_conflict="k").execute()


# ------------------------------------------------------------------ applications
def get_app(tg_id: int, refnr: str) -> Optional[dict]:
    r = (sb.table(T_APPS).select("*").eq("tg_id", tg_id).eq("refnr", refnr).limit(1).execute())
    return r.data[0] if r.data else None


def _job_row(tg_id: int, job: dict) -> dict:
    return {
        "tg_id": tg_id, "refnr": job["refnr"],
        "employer": job.get("employer"), "employer_key": employer_key(job.get("employer")),
        "title": job.get("title"), "to_email": job.get("email"),
    }


def upsert_app(tg_id: int, job: dict, **fields) -> Optional[dict]:
    row = {**_job_row(tg_id, job), "updated_at": now_iso(), **fields}
    r = sb.table(T_APPS).upsert(row, on_conflict="tg_id,refnr").execute()
    return r.data[0] if r.data else None


def insert_queued(tg_id: int, job: dict, letter: dict, send_after: datetime) -> Optional[dict]:
    """Insert an auto application. Returns None if this user already has a row for the job."""
    row = {**_job_row(tg_id, job), "letter": letter, "mode": "auto", "status": "queued",
           "send_after": send_after.isoformat()}
    r = sb.table(T_APPS).upsert(row, on_conflict="tg_id,refnr", ignore_duplicates=True).execute()
    return r.data[0] if r.data else None


def set_status(tg_id: int, refnr: str, status: str, only_from: Optional[list] = None, **extra) -> list:
    """Compare-and-set status. Returns updated rows (empty list = precondition failed)."""
    q = (sb.table(T_APPS).update({"status": status, "updated_at": now_iso(), **extra})
         .eq("tg_id", tg_id).eq("refnr", refnr))
    if only_from:
        q = q.in_("status", only_from)
    return q.execute().data

def set_app_sender(app_id: int, sender_email: str) -> None:
    sb.table(T_APPS).update({"sender_email": sender_email}).eq("id", app_id).execute()

def delete_app(tg_id: int, refnr: str) -> None:
    sb.table(T_APPS).delete().eq("tg_id", tg_id).eq("refnr", refnr).execute()


def known_refnrs(tg_id: int) -> list:
    """Jobs the user already handled (anything except an open draft)."""
    r = sb.table(T_APPS).select("refnr").eq("tg_id", tg_id).neq("status", "draft").limit(5000).execute()
    return [x["refnr"] for x in r.data]


def employer_recent(tg_id: int, ekey: str, days: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = (sb.table(T_APPS).select("id", count="exact").eq("tg_id", tg_id).eq("employer_key", ekey)
         .in_("status", list(ACTIVE)).gte("created_at", cutoff).limit(1).execute())
    return (r.count or 0) > 0


def count_auto_today(tg_id: int) -> int:
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    r = (sb.table(T_APPS).select("id", count="exact").eq("tg_id", tg_id).eq("mode", "auto")
         .in_("status", ["queued", "sending", "sent"])
         .gte("created_at", start.astimezone(timezone.utc).isoformat()).limit(1).execute())
    return r.count or 0


def claim_due(batch: int) -> list:
    return sb.rpc("deutschjob_claim_due", {"batch": batch}).execute().data or []


def mark_sent(app_id: int, to_email: str, message_id: Optional[str] = None) -> None:
    (sb.table(T_APPS).update({"status": "sent", "sent_at": now_iso(), "error": None,
                              "to_email": to_email, "message_id": message_id, "updated_at": now_iso()})
     .eq("id", app_id).execute())


def mark_failed(app_id: int, error: str) -> None:
    (sb.table(T_APPS).update({"status": "failed", "error": error[:500], "updated_at": now_iso()})
     .eq("id", app_id).execute())


def history(tg_id: int, limit: int = 10) -> list:
    r = (sb.table(T_APPS).select("title,employer,status,mode,created_at,sent_at,to_email,error,outcome")
         .eq("tg_id", tg_id)
         .neq("status", "skipped").order("created_at", desc=True).limit(limit).execute())
    return r.data


# ------------------------------------------------------------------ sender (per-client mailbox)
def has_sender(u: Optional[dict]) -> bool:
    return bool(u and u.get("smtp_user") and u.get("smtp_pass_enc"))


def set_sender(tg_id: int, host: str, port: int, user: str, pass_enc: str, from_name: Optional[str]) -> None:
    update_user(tg_id, smtp_host=host, smtp_port=port, smtp_user=user, smtp_pass_enc=pass_enc,
                from_name=from_name, sender_verified_at=now_iso())


def clear_sender(tg_id: int) -> None:
    update_user(tg_id, smtp_host=None, smtp_port=None, smtp_user=None, smtp_pass_enc=None,
                from_name=None, sender_verified_at=None)


# ------------------------------------------------------------------ stats (SQL view deutschjob_client_stats)
def client_stats(tg_id: Optional[int] = None) -> list:
    q = sb.table("deutschjob_client_stats").select("*")
    if tg_id is not None:
        q = q.eq("tg_id", tg_id)
    return q.execute().data

# ------------------------------------------------------------------ reply tracking
T_REPLIES = "deutschjob_replies"
T_BAD = "deutschjob_bad_emails"


def list_reply_users() -> list:
    r = sb.table(T_USERS).select("*").eq("track_replies", True).execute()
    return [u for u in r.data if has_sender(u)]


def sent_apps_since(tg_id: int, since_iso: str) -> list:
    r = (sb.table(T_APPS).select("id,refnr,title,employer,to_email,message_id,sent_at,outcome")
         .eq("tg_id", tg_id).eq("status", "sent").gte("sent_at", since_iso)
         .order("sent_at").limit(2000).execute())
    return r.data


def seen_message_ids(tg_id: int) -> list:
    r = sb.table(T_REPLIES).select("message_id").eq("tg_id", tg_id).limit(10000).execute()
    return [x["message_id"] for x in r.data]


def add_reply(tg_id: int, message_id: str, app_id: Optional[int], category: str,
              received_at: Optional[str]) -> bool:
    """Records a processed inbox message. Returns False if it was already recorded (dedupe gate)."""
    row = {"tg_id": tg_id, "message_id": message_id, "app_id": app_id,
           "category": category, "received_at": received_at}
    r = sb.table(T_REPLIES).upsert(row, on_conflict="tg_id,message_id", ignore_duplicates=True).execute()
    return bool(r.data)


def set_outcome(app_id: int, outcome: str, summary: Optional[str], reply_at: Optional[str]) -> None:
    (sb.table(T_APPS).update({"outcome": outcome, "reply_summary": (summary or "")[:500],
                              "reply_at": reply_at or now_iso(), "updated_at": now_iso()})
     .eq("id", app_id).execute())


def add_bad_email(addr: str, reason: str) -> None:
    sb.table(T_BAD).upsert({"email": addr.lower(), "reason": reason[:200]}, on_conflict="email").execute()


def bad_emails() -> set:
    r = sb.table(T_BAD).select("email").limit(50000).execute()
    return {x["email"] for x in r.data}
