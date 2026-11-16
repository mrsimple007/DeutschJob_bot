"""CV files in Supabase Storage (private bucket)."""
from db import sb
from config import log

BUCKET = "deutschjob-cvs"

_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_EXTS = tuple(_MIME)


def _key(uid: int, ext: str) -> str:
    return f"{uid}/cv{ext}"


def upload_cv(uid: int, data: bytes, ext: str) -> str:
    """Store the user's CV (replacing any earlier one). Returns the storage key."""
    ext = ext.lower()
    bucket = sb.storage.from_(BUCKET)
    # remove an older CV with a different extension (e.g. old .pdf when a .docx is uploaded)
    others = [_key(uid, e) for e in _EXTS if e != ext]
    try:
        bucket.remove(others)
    except Exception:
        pass
    key = _key(uid, ext)
    bucket.upload(key, data, {"content-type": _MIME.get(ext, "application/octet-stream"),
                              "upsert": "true"})
    return key


def download_cv(key: str) -> bytes:
    return sb.storage.from_(BUCKET).download(key)


def delete_user_files(uid: int) -> None:
    bucket = sb.storage.from_(BUCKET)
    files = bucket.list(str(uid)) or []
    paths = [f"{uid}/{f['name']}" for f in files if f.get("name")]
    if paths:
        bucket.remove(paths)
    log.info("[gdpr] removed %d stored file(s) of user %s", len(paths), uid)