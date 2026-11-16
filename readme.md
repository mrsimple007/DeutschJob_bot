# JobSimple: AI job-application assistant for Germany

A Telegram bot that reads a user's CV, finds matching vacancies in Germany, writes a tailored German cover letter (Anschreiben) with AI, and sends applications by email.

> **Status:** working MVP (manual mode). Auto mode, database storage and multi-user hardening are designed but not yet built. See [Roadmap](#roadmap).

---

## Table of contents

1. [What it does](#what-it-does)
2. [Current status](#current-status)
3. [How it works](#how-it-works)
4. [Tech stack](#tech-stack)
5. [Project layout](#project-layout)
6. [Setup](#setup)
7. [Configuration](#configuration)
8. [Bot commands](#bot-commands)
9. [Job data source: Bundesagentur API](#job-data-source-bundesagentur-api)
10. [Gemini usage and key rotation](#gemini-usage-and-key-rotation)
11. [Auto mode design](#auto-mode-design)
12. [Database schema](#database-schema)
13. [Email sending options](#email-sending-options)
14. [Safety, limits and compliance](#safety-limits-and-compliance)
15. [Troubleshooting](#troubleshooting)
16. [Roadmap](#roadmap)

---

## What it does

1. The user sends a CV (PDF or DOCX) to the bot.
2. Gemini extracts a structured profile: name, skills, seniority, German level and 4-6 short German job titles to search for.
3. The bot searches the Bundesagentur für Arbeit job board and lists the found vacancies with the key information: title, employer, place, matched skills, a warning if strong German is required, contact email if present, and a link.
4. When the user presses **Apply**, Gemini writes a German cover letter based only on facts from the CV.
5. The user previews the letter and then:
   - **Job has an email address:** presses **Send**, and the bot emails the letter and CV to the employer.
   - **Job has only a web form:** receives the letter as text and a DOCX file plus a button to open the application page, and applies there.

Searching costs **no AI tokens**. Gemini is called only for CV parsing (once per CV) and for cover letters (once per Apply).

---

## Current status

| Feature | Status |
|---|---|
| CV parsing (PDF/DOCX) to profile | Done |
| Job search via Bundesagentur v6 API | Done |
| Free local scoring (skill keywords, German-level warning) | Done |
| Cover letter generation on Apply | Done |
| Preview, rewrite, cancel, send by email | Done |
| DOCX letter for web-form jobs | Done |
| Multiple Gemini API keys with automatic rotation | Done |
| Step-by-step logging | Done |
| Persistent database (SQLite) | Designed |
| Auto mode (scheduled scan, queue, delayed send) | Designed |
| Per-user Gmail OAuth or domain sending | Designed |
| `/note` command (extra context for letters) | Planned |
| `/delete` command (GDPR data removal) | Planned |
| Browser automation for web forms (Playwright) | Planned, later |

---

## How it works

### Manual flow

```
User sends CV
   |
   v
Gemini parses CV -> profile (JSON)
   |
   v
/search -> Bundesagentur API (v6) -> jobs + details
   |
   v
Local scoring (no AI): skills found in ad, German warning
   |
   v
Bot shows jobs with buttons: [Apply] [Open] [Skip]
   |
   v
Apply -> Gemini writes Anschreiben -> preview
   |
   +-- job has email -> [Send] [Rewrite] [Cancel] -> SMTP email with CV attached
   |
   +-- no email      -> DOCX + [Open application page] [Mark as applied]
```

### Why there is no AI ranking

Ranking 15 job descriptions with an LLM on every search was slow (30+ seconds) and used many tokens. A local check (does the ad mention the candidate's skills and job-title keywords, does it demand C1 German) is free, instant and good enough to sort results.

---

## Tech stack

- **Python 3.9+**
- **python-telegram-bot** (v20+, async) for the interface
- **Google Gemini API** (`google-generativeai`) for CV parsing and cover letters
- **httpx** for the Bundesagentur API
- **python-docx** for reading DOCX CVs and creating letter files
- **smtplib** for sending email (replaceable, see [Email sending options](#email-sending-options))
- **python-dotenv** for configuration

`requirements.txt`:

```
python-telegram-bot[job-queue]>=20.0
google-generativeai
python-docx
httpx
python-dotenv
```

---

## Project layout

Current state is a single `bot.py`. The recommended structure once auto mode is added:

```
jobbot/
  main.py          # builds the bot, registers handlers, schedules jobs
  config.py        # environment variables
  db.py            # SQLite now, Postgres later
  llm.py           # KeyPool, gemini_json, CV parsing, letter writing
  jobs.py          # Bundesagentur API client, find_jobs, email extraction
  matching.py      # quick_info(): free local scoring
  mailer.py        # send(profile, job, letter, cv_path)
  autopilot.py     # scan and send workers
  handlers.py      # Telegram commands and buttons
cvs/               # uploaded CVs and generated letters (git-ignored)
jobbot.db          # database (git-ignored)
.env               # secrets (git-ignored)
```

Telegram's `PicklePersistence` is fine for the manual MVP, but scheduled workers run outside any chat and cannot read `ctx.user_data`, so user data moves to the database when auto mode is added.

---

## Setup

```bash
git clone <your-repo>
cd JobSimple
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the values
python bot.py
```

1. Create a bot with **@BotFather** in Telegram and copy the token.
2. Create Gemini API keys in **Google AI Studio**. Several keys are supported.
3. For Gmail SMTP, enable 2-step verification and create an **app password**. Do not use the normal account password.
4. Keep `DRY_RUN=1` until you have checked a few real letters in your own inbox.

Add to `.gitignore`:

```
.env
cvs/
botdata.pkl
jobbot.db
venv/
```

---

## Configuration

All settings are environment variables, loaded from `.env`.

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather | required |
| `GEMINI_API_KEY` | First Gemini key | required |
| `GEMINI_API_KEY_1` ... `GEMINI_API_KEY_20` | Extra keys for rotation | optional |
| `GEMINI_MODEL` | Gemini model name | `gemini-3.6-flash` |
| `SMTP_HOST` | SMTP server | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP SSL port | `465` |
| `SMTP_USER` | Sender account | |
| `SMTP_PASS` | SMTP password or app password | |
| `DRY_RUN` | `1` sends all emails to `SMTP_USER` instead of employers | `1` |
| `MAX_SHOW` | Maximum jobs shown per search | `10` |

Example `.env`:

```dotenv
TELEGRAM_TOKEN=
GEMINI_API_KEY=
GEMINI_API_KEY_1=
GEMINI_API_KEY_2=
GEMINI_MODEL=gemini-3.6-flash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=
SMTP_PASS=
DRY_RUN=1
MAX_SHOW=10
```

---

## Bot commands

| Command | Description |
|---|---|
| `/start` | Show help |
| *(send a PDF/DOCX)* | Upload and parse a CV |
| `/profile` | Show the parsed profile |
| `/location Berlin` | Search near a German city |
| `/location clear` | Search all of Germany (default) |
| `/search` | Find jobs and show them with buttons |
| `/auto on\|off` | *(planned)* turn auto mode on or off |
| `/auto limit N` | *(planned)* daily application limit, 1-10 |
| `/note <text>` | *(planned)* add context for letters |
| `/delete` | *(planned)* delete all stored data |

Buttons under each job: **Apply**, **Open**, **Skip**. Under a letter draft: **Send**, **Rewrite**, **Cancel** (email jobs) or **Open application page**, **Rewrite**, **Mark as applied** (web-form jobs).

**Important:** the location extracted from the CV is where the candidate lives now, which may not be in Germany. It is not used for searching. Only a location set with `/location` is used.

---

## Job data source: Bundesagentur API

The bot uses the public Jobsuche API of the Bundesagentur für Arbeit (community documentation: [bundesAPI/jobsuche-api](https://github.com/bundesAPI/jobsuche-api)).

- **Base URL:** `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service`
- **Header:** `X-API-Key: jobboerse-jobsuche` (a public client ID, no signup)
- **Search:** `GET /pc/v6/jobs`
  - Parameters: `was` (keywords), `wo` (place), `umkreis` (radius in km), `size`, `page`, `angebotsart=1`
  - Result list is under `ergebnisliste`
  - Fields used: `referenznummer`, `stellenangebotsTitel`, `firma`, `stellenlokationen[].adresse.ort`, `externeUrl`
- **Details:** `GET /pc/v4/jobdetails/{base64(referenznummer)}`
  - Field used: `stellenangebotsBeschreibung`
- **Job page:** `https://www.arbeitsagentur.de/jobsuche/jobdetail/{referenznummer}`

Notes:

- The old `/pc/v4/jobs` search endpoint returns HTTP 403. The code tries `v6`, then `v4/app`, then `v4`, and remembers the one that works.
- The API is unofficial and can change without notice.
- English job titles return almost nothing. The CV prompt asks for short German titles as they appear in German job ads (for example "Disponent", "Logistikmitarbeiter"). If a title finds nothing, the code retries with single keywords.
- Contact emails are found by scanning the details response with a regex. Only a minority of listings contain one, because most employers use their own application portal.

Avoid scraping LinkedIn, Indeed, StepStone or XING. It violates their terms and gets blocked.

---

## Gemini usage and key rotation

`gemini_json()` is the single entry point for all model calls. It uses a `KeyPool`:

- **Quota or rate limit (429):** switch to the next key, rest the exhausted one for 60 seconds.
- **Invalid or unauthorized key:** disable that key for the session.
- **Temporary errors (503, timeout):** retry with exponential backoff.
- **Empty or malformed JSON:** retry.
- **All keys rate-limited:** wait for the earliest cooldown instead of failing.

Keys are read from `GEMINI_API_KEY` and `GEMINI_API_KEY_1` to `_20`.

Calls are serialized with a lock because `genai.configure()` is global. This is fine for one user and a few testers. For many users, migrate to the newer `google-genai` SDK, which creates one client per key and can run calls in parallel.

**Token cost per user action:**

| Action | Gemini calls |
|---|---|
| Upload CV | 1 |
| `/search` | 0 |
| Apply (letter draft) | 1 |
| Rewrite | 1 |
| Send | 0 |

**Privacy:** as far as I know, free-tier API content may be used by Google to improve its products. Verify the current terms and use a paid tier before handling real clients' CVs.

---

## Auto mode design

*Designed, not yet implemented.* A scheduled scan runs for each user who turned auto mode on.

1. **Find jobs.** Search results are cached for about 3 hours and shared between users with the same titles and location.
2. **Filter without AI:**
   - job has an email address (not `noreply`, `datenschutz`, and similar)
   - not already applied to by this user
   - no application to the same employer within 30 days
   - no strong German requirement (C1+)
   - local score above the user's threshold
3. **Check limits.** Daily limit per user (default 5, maximum 10).
4. **Write the letter** with Gemini.
5. **Queue it** to send in 30 minutes plus 0-10 minutes of random jitter. The user gets a Telegram message with a **Cancel** button.
6. **Send** only on weekdays, 08:00-17:00 Berlin time. A row is claimed (`queued` to `sending`) before sending so it can never go out twice. After sending, the user is notified.

Schedule:

- `scan_all`: every 3 hours
- `send_due`: every minute

Consent is required before enabling auto mode and is stored with a timestamp. Auto mode letters are not reviewed by a human before the cancel window ends, so letter quality will sometimes be mediocre. Keep the limits and the review window.

---

## Database schema

SQLite to start, Postgres later.

```sql
CREATE TABLE users(
  tg_id INTEGER PRIMARY KEY,
  profile TEXT,            -- JSON from CV parsing
  cv_path TEXT,
  location TEXT,
  auto_on INTEGER DEFAULT 0,
  daily_limit INTEGER DEFAULT 5,
  min_score INTEGER DEFAULT 4,
  consent_at REAL,
  created REAL
);

CREATE TABLE applications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER,
  refnr TEXT,
  employer TEXT,
  title TEXT,
  to_email TEXT,
  letter TEXT,             -- JSON {subject, body}
  status TEXT,             -- queued | sending | sent | failed | cancelled
  send_after REAL,
  sent_at REAL,
  error TEXT,
  created REAL,
  UNIQUE(tg_id, refnr)     -- never apply twice to the same job
);

CREATE TABLE search_cache(
  k TEXT PRIMARY KEY,      -- "<location>|<sorted titles>"
  data TEXT,
  ts REAL
);
```

Manual applications should also be written to `applications` so auto mode never duplicates them.

---

## Email sending options

Sending all clients' applications from one personal Gmail account will get it flagged as spam quickly, and replies would go to the wrong inbox. In order of preference:

1. **Gmail API with OAuth per user.** Mail is sent from the candidate's own address and appears in their Sent folder. Replies go directly to them. Best deliverability. Google requires app verification for the `gmail.send` scope.
2. **Your own domain** with a transactional provider (Resend, Postmark, Amazon SES). Configure SPF, DKIM and DMARC. Send from an address such as `anna.mueller@yourdomain.de` with the candidate in Reply-To.
3. **Single Gmail SMTP.** Testing only.

Keep one interface, `send(profile, job, letter, cv_path)`, so the implementation can be swapped without touching the rest.

---

## Safety, limits and compliance

- **`DRY_RUN=1` by default.** Emails go to the sender's own inbox until switched off deliberately.
- **No invented facts.** The letter prompt allows only information from the CV profile: no made-up employers, degrees, numbers, certificates, language levels or start dates.
- **Human review in manual mode.** Every letter is previewed before sending.
- **Cancel window in auto mode.** 30 minutes plus jitter before anything is sent.
- **Rate limits.** Daily limit per user, one application per employer per 30 days, random delays of 20-60 seconds between sends, working hours only.
- **Recipient check.** Skip `noreply`, `datenschutz`, `privacy`, `abuse` addresses. Log every recipient. A wrong address means someone's CV reaches a stranger.
- **GDPR.**
  - Store the consent timestamp.
  - Provide `/delete` to remove CV, profile and history.
  - Keep an application log.
  - Tell users that their CV is sent to employers and processed by Google's Gemini API.
- **Secrets.** Never commit `.env` or tokens. If a token ever appears in a log or chat, revoke it with `/revoke` in @BotFather and create a new one. The bot sets the `httpx` logger to WARNING because its INFO logs print the full Telegram URL, including the bot token.
- **Platform rules.** Do not automate LinkedIn, Indeed or similar sites. Their terms forbid it.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `403 No match found for request` on search | Old `/pc/v4/jobs` endpoint is gone. Use `/pc/v6/jobs` (the code already falls back through several endpoints). |
| `unknown response shape` and 0 hits | v6 returns the list under `ergebnisliste`. Make sure `extract_hits` handles it. A response with only `maxErgebnisse`, `page`, `size` means a genuinely empty result. |
| 0 hits for every title | Titles are in English. Re-send the CV so the bot extracts short German titles. Old titles are stored in `botdata.pkl` and are not regenerated by `/search`. |
| Search for the candidate's home city finds nothing | The CV location may be outside Germany. Use `/location <German city>` or `/location clear`. |
| Gemini `quota/rate limit` in logs | Normal on free keys. The bot switches keys automatically. Add more keys or move to a paid tier. |
| `All Gemini API keys are invalid` | Check the key values in `.env` and that the Gemini API is enabled for them. |
| Telegram message formatting breaks | Job text contains special characters. The bot uses HTML mode with escaping. Do not switch to Markdown mode. |
| Email sending fails | Use a Gmail app password, port 465 with SSL. Check `SMTP_USER` and `SMTP_PASS`. |
| Few jobs have the Email field | Expected. Most employers use their own portals. This is the limit of email auto-apply. |

Log lines are tagged by stage: `[tg]`, `[ba]`, `[jobs]`, `[gemini]`, `[mail]`, `[auto]`. Each step logs start, end and duration.

---

## Roadmap

1. **Database.** Move users and applications from pickle to SQLite.
2. **Auto mode.** Scheduled scan, queue, cancel window, send worker.
3. **`/note` command.** Let users add one line of context (availability, willingness to relocate) that is passed into the letter prompt.
4. **`/delete` command.** Full data removal.
5. **Better email sending.** Gmail OAuth per user or a verified sending domain.
6. **Parallel Gemini.** Migrate to the `google-genai` SDK with one client per key.
7. **Translation view.** Show an English translation next to the German letter for users with weak German.
8. **Web-form automation.** Playwright adapters for the 2-3 most common application systems (for example Personio). Gemini only maps unfamiliar fields, a screenshot is taken before submitting, and the bot falls back to the manual flow on CAPTCHAs, account creation or unknown required fields. Run in a separate worker process.
9. **Web dashboard.** Application tracker and settings (HTML/JS front end over the same database).
10. **Postgres and separate worker process** once the scan takes more than a few minutes.

---

## Disclaimer

This tool sends personal data and documents to third parties on a user's behalf. Operators are responsible for obtaining user consent, complying with GDPR and following the terms of every service they connect to. AI-written letters can contain mistakes. Review them before real use.