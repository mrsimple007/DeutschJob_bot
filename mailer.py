"""Email sending from each client's own mailbox (SMTP + app password).
Mails come from the client's address, land in their Sent folder, and replies go straight to them.
Keep send() stable so a Gmail-OAuth or transactional backend can replace it later."""
import smtplib, ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

from pathlib import Path

import crypto, letters, storage
from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, DRY_RUN, ALLOW_SHARED_SENDER, log
from jobs import is_bad_email

# domain -> (smtp host, port). 465 = SSL, anything else = STARTTLS.
PROVIDERS = {
    "gmail.com": ("smtp.gmail.com", 465), "googlemail.com": ("smtp.gmail.com", 465),
    "gmx.de": ("mail.gmx.net", 465), "gmx.net": ("mail.gmx.net", 465), "gmx.com": ("mail.gmx.net", 465),
    "web.de": ("smtp.web.de", 587),
    "t-online.de": ("securesmtp.t-online.de", 465),
    "yahoo.com": ("smtp.mail.yahoo.com", 465), "yahoo.de": ("smtp.mail.yahoo.com", 465),
    "icloud.com": ("smtp.mail.me.com", 587), "me.com": ("smtp.mail.me.com", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587), "outlook.de": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587), "hotmail.de": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
}


class SenderNotConfigured(RuntimeError):
    pass


def guess_smtp(email: str) -> Optional[tuple]:
    return PROVIDERS.get(email.split("@")[-1].lower())


def _connect(host: str, port: int, user: str, password: str) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls(context=ctx)
    s.login(user, password)
    return s


def verify(host: str, port: int, user: str, password: str) -> None:
    """Blocking. Raises if the login is rejected. Sends nothing."""
    with _connect(host, port, user, password):
        pass


def _resolve_sender(u: dict) -> dict:
    if u.get("smtp_user") and u.get("smtp_pass_enc"):
        return {"host": u["smtp_host"], "port": int(u["smtp_port"]), "user": u["smtp_user"],
                "password": crypto.decrypt(u["smtp_pass_enc"]), "name": u.get("from_name")}
    if SMTP_USER and SMTP_PASS and (DRY_RUN or ALLOW_SHARED_SENDER):
        return {"host": SMTP_HOST, "port": SMTP_PORT, "user": SMTP_USER, "password": SMTP_PASS, "name": None}
    raise SenderNotConfigured("No sender connected. Use /sender to connect your own email account.")


def _deliver(sender: dict, msg: EmailMessage) -> None:
    with _connect(sender["host"], sender["port"], sender["user"], sender["password"]) as s:
        s.send_message(msg)


MAX_MAIL_BYTES = 22 * 1024 * 1024    # raw attachments; base64 adds ~33%, providers cap at ~25 MB


def application_files(user: dict, job: dict, letter: dict) -> list:
    """Blocking. Everything attached to an application, in the order German recruiters expect:
    Anschreiben (PDF), Lebenslauf, then the client's extra files. Items: (filename, bytes, mime)."""
    profile = user.get("profile") or {}
    name = letters.safe_name(profile.get("full_name") or "")
    files = []
    try:
        files.append((f"Anschreiben_{name}.pdf", letters.letter_pdf(profile, job, letter), "application/pdf"))
    except Exception:
        log.exception("[mail] cover letter PDF failed, sending without it")   # the letter is in the body anyway
    ext = Path(user["cv_path"]).suffix
    files.append((f"Lebenslauf_{name}{ext}", storage.download_cv(user["cv_path"]), storage.cv_mime(ext)))
    for a in user.get("attachments") or []:
        files.append((a["name"], storage.download(a["key"]), a.get("mime") or "application/octet-stream"))
    total = sum(len(f[1]) for f in files)
    if total > MAX_MAIL_BYTES:
        raise ValueError(f"Attachments too large ({total // 1048576} MB). Remove some with /files.")
    return files


def send(user: dict, job: dict, letter: dict, files: list) -> tuple:
    """Blocking. `user` is the deutschjob_users row, `files` comes from application_files().
    Returns (address actually mailed, Message-ID) - the Message-ID lets reply tracking match replies."""
    profile = user.get("profile") or {}
    sender = _resolve_sender(user)
    if not DRY_RUN and (not job.get("email") or is_bad_email(job["email"])):
        raise ValueError(f"Refusing to send to suspicious address: {job.get('email')!r}")
    to_addr = sender["user"] if DRY_RUN else job["email"]
    log.info("[mail] %s -> %s via %s as %s (dry_run=%s, %d attachment(s))", job["refnr"], to_addr,
             sender["host"], sender["user"], DRY_RUN, len(files))

    msg = EmailMessage()
    msg["From"] = formataddr((sender["name"] or profile.get("full_name") or "", sender["user"]))
    msg["To"] = to_addr
    msg["Message-ID"] = make_msgid(domain=sender["user"].rsplit("@", 1)[-1])
    cv_mail = (profile.get("email") or "").strip()
    # with reply tracking on, replies must land in the tracked (sender) mailbox, so no Reply-To
    if cv_mail and cv_mail.lower() != sender["user"].lower() and not user.get("track_replies"):
        msg["Reply-To"] = cv_mail
    msg["Subject"] = ("[TEST] " if DRY_RUN else "") + letter["subject"]
    msg.set_content(letter["body"])
    for filename, data, mime in files:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    _deliver(sender, msg)
    log.info("[mail] sent OK")
    return to_addr, msg["Message-ID"]


def send_test(user: dict) -> str:
    """Sends a short test mail from the client's mailbox to itself."""
    sender = _resolve_sender(user)
    msg = EmailMessage()
    msg["From"] = sender["user"]
    msg["To"] = sender["user"]
    msg["Subject"] = "JobSimple: sender test"
    msg.set_content("Your mailbox is connected. Applications will be sent from this address.")
    _deliver(sender, msg)
    return sender["user"]