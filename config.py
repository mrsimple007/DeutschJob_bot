import os, re, time, logging
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import dotenv

dotenv.load_dotenv()
import re   # at the top, if not already imported

ADMIN_IDS = {int(x) for x in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()}

# ------------------------------------------------------------------ logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs full URLs at INFO, which includes the Telegram bot token. Silence it.
for noisy in ("httpx", "httpcore", "telegram", "hpack", "postgrest", "supabase", "apscheduler",
              "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("jobbot")

MASTER_SHEET_ID = os.getenv("MASTER_SHEET_ID", "")


@contextmanager
def step(name: str):
    """Log start/end/duration of a processing step."""
    t0 = time.time()
    log.info("▶ %s ...", name)
    try:
        yield
    except Exception:
        log.error("✖ %s FAILED after %.1fs", name, time.time() - t0)
        raise
    else:
        log.info("✔ %s done in %.1fs", name, time.time() - t0)


GOOGLE_SA_FILE = os.getenv("GOOGLE_SA_FILE", "service_account.json")
# ------------------------------------------------------------------ secrets / env
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]   # service_role key, server-side only

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
# used when MODEL is overloaded (503/504). Empty = no fallback. A wrong name is detected (404) and ignored.
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest").strip()
if FALLBACK_MODEL == MODEL:
    FALLBACK_MODEL = ""
USER_WAIT_S = 90                      # max seconds a user waits for Gemini (CV parsing, manual letters)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
MAX_SHOW = int(os.getenv("MAX_SHOW", "10"))

# ------------------------------------------------------------------ auto mode
TZ = ZoneInfo("Europe/Berlin")
WORK_START, WORK_END = 8, 17          # sending window, Berlin time, Mon-Fri
CANCEL_WINDOW_MIN = 30                # letter is held this long before sending
JITTER_MAX_MIN = 10                   # plus 0-10 random minutes
SCAN_INTERVAL_S = 3 * 3600            # scan_all every 3 hours
SEND_INTERVAL_S = 60                  # send_due every minute
SEND_BATCH = 2                        # max emails claimed per minute
SEND_GAP = (20, 60)                   # random pause (s) between two sends
CACHE_TTL_H = 3
EMPLOYER_COOLDOWN_DAYS = 30

# ------------------------------------------------------------------ matching / jobs
LEVELS = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "NATIVE": 7}
GER_REQ_RE = re.compile(r"\b(C1|C2)\b|verhandlungssicher|flie(ß|ss)end|muttersprach", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

BA_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
SEARCH_PATHS = ["/pc/v6/jobs", "/pc/v4/app/jobs", "/pc/v4/jobs"]
DETAIL_PATHS = ["/pc/v4/jobdetails/{}", "/pc/v3/jobdetails/{}"]

# ------------------------------------------------------------------ storage / security / admin
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "deutschjob-cvs")
ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]   # Fernet key, encrypts clients' SMTP passwords
ADMIN_IDS = {int(x) for x in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()}
# Shared sender (SMTP_USER above) is used only in DRY_RUN, or if you explicitly allow it.
ALLOW_SHARED_SENDER = os.getenv("ALLOW_SHARED_SENDER", "0") == "1"
DIGEST_HOUR = 18   # daily admin summary, Berlin time
# ------------------------------------------------------------------ reply tracking (IMAP)
REPLY_CHECK_INTERVAL_S = 30 * 60      # check opted-in inboxes every 30 minutes
REPLY_LOOKBACK_DAYS = 60              # only match replies to applications sent in this window
REPLY_MAX_SCAN = 300                  # newest inbox messages scanned (headers only) per check

# ------------------------------------------------------------------ profiles
MAX_PROFILES = 10                     # candidate profiles one Telegram account can manage
