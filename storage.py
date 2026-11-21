"""CV files and extra attachments in Supabase Storage (private bucket)."""
import re, uuid

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


# ------------------------------------------------------------------ extra attachments (Zeugnisse etc.)
ATT_MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
ATT_MAX_FILES = 5
ATT_MAX_FILE = 5 * 1024 * 1024       # per file
ATT_MAX_TOTAL = 15 * 1024 * 1024     # all extra files together (mail limit is ~25 MB incl. encoding)


def cv_mime(ext: str) -> str:
    return _MIME.get(ext.lower(), "application/octet-stream")


def upload_attachment(uid: int, data: bytes, filename: str) -> dict:
    """Stores one extra file. Returns its metadata for deutschjob_users.attachments."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    aid = uuid.uuid4().hex[:8]
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[-80:] or f"file{ext}"
    key = f"{uid}/att/{aid}_{ascii_name}"
    sb.storage.from_(BUCKET).upload(key, data, {"content-type": ATT_MIME.get(ext, "application/octet-stream"),
                                               "upsert": "true"})
    return {"id": aid, "name": filename[:80], "key": key, "size": len(data),
            "mime": ATT_MIME.get(ext, "application/octet-stream")}


def download(key: str) -> bytes:
    return sb.storage.from_(BUCKET).download(key)


def delete_keys(keys: list) -> None:
    if keys:
        sb.storage.from_(BUCKET).remove(keys)


def delete_user_files(uid: int) -> None:
    bucket = sb.storage.from_(BUCKET)
    paths = []
    for folder in (str(uid), f"{uid}/att"):
        for f in bucket.list(folder) or []:
            if f.get("name") and f.get("id"):          # entries without id are sub-folders
                paths.append(f"{folder}/{f['name']}")
    if paths:
        bucket.remove(paths)
    log.info("[gdpr] removed %d stored file(s) of user %s", len(paths), uid)