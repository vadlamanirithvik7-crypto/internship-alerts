# Internship Alert System

Watches a large set of company job boards and job feeds for new **internship and
co-op** postings, tags them by sector, and alerts you when something matches your
saved filters. Everything runs on free APIs — no paid services, no LLM/API credits.

Sectors tracked: Software/Tech, Semiconductor, Computer Architecture, Power
Electronics, Robotics, and a general Hardware bucket.

---

## How it works

```
GitHub Actions (every 30 min)          Render (free web service)
  poller/main.py                          backend/  FastAPI + HTMX
   ├─ harvest sources                       ├─ browse & filter postings
   ├─ tag sectors (keyword rules)           ├─ manage alert filters
   ├─ dedupe + store                        └─ inspect company watchlist
   └─ match filters → alert
              │                                     │
              └──────────► Supabase Postgres ◄──────┘
                                  │
                     Email (Gmail SMTP) + ntfy.sh push
```

### Where postings come from

Three layers, so coverage isn't capped by any single list:

1. **Community tracker feeds** — `SimplifyJobs/Summer2026-Internships`,
   `vanshb03/Summer2026-Internships`, `SimplifyJobs/New-Grad-Positions`. Bot-updated
   JSON with direct application links. Highest-yield source, ~3 HTTP requests.
2. **Direct company boards** — Greenhouse, Lever, Ashby and Workday public JSON
   APIs, polled per company. Usually the fastest path from "posted" to "alerted".
3. **Broad keyword search** — Adzuna, USAJOBS, Arbeitnow, RemoteOK. Not bounded by
   the watchlist, so it catches small/private companies the other layers miss.

### How the company watchlist grows (no hand-maintained list)

- Every posting URL is parsed to detect its ATS and **learn a real board slug**
  (`boards.greenhouse.io/x`, `x.wd1.myworkdayjobs.com`, …). Seeing a company once
  is enough to start polling it directly forever after.
- **SEC EDGAR SIC industry browse** discovers companies by sector (e.g. SIC 3674 =
  semiconductors) — free, keyless, hundreds of real companies per sector.
  (ETF holdings were the original plan, but iShares/VanEck serve bot-block pages to
  non-browser clients and only expose ~30 top holdings anyway.)
- `poller/resolver.py` probes candidate slugs against the **live** ATS APIs and only
  keeps a slug the API actually confirms. Nothing guessed is ever stored as fact.
  Unresolved companies stay in the list and are still matched by name via keyword search.

The first run seeds ~1,200 companies; the weekly discovery job grows it from there.

---

## Setup

### 1. Local install

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the values below
```

Runs on SQLite locally with zero config. Try it:

```bash
python3 poller/main.py --board-slice 25 --skip-alerts --verbose
python3 -m uvicorn backend.main:app --reload --port 8000
# open http://127.0.0.1:8000
```

### 2. Supabase (shared database)

Needed so the GitHub Actions poller and the Render web app read/write the same data.

1. Create a free project at <https://supabase.com>.
2. Project Settings → Database → Connection string → **URI** (use the *pooled*
   connection, port 6543, for serverless clients).
3. That string is your `DATABASE_URL`. Tables are created automatically on first run.

### 3. Alert channels

**ntfy push (no signup):** pick a long, unguessable topic name — the topic *is* the
secret. Install the ntfy app (iOS/Android) and subscribe to it. Set `NTFY_TOPIC`.

**Email:** enable 2FA on a Gmail account, then create an App Password at
<https://myaccount.google.com/apppasswords>. Set `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`
and `GMAIL_APP_PASSWORD` (the App Password, never your real one).

### 4. Free job API keys (optional but recommended)

| Service | Where | Notes |
|---|---|---|
| Adzuna | <https://developer.adzuna.com/> | free tier, no card. Biggest win of the keyword layer. |
| USAJOBS | <https://developer.usajobs.gov/apirequest/> | free, email signup. NASA / national labs / DoD internships. |

`SEC_CONTACT_EMAIL` is not an API key — just your email address. SEC EDGAR **rejects
requests with a 403** unless the User-Agent carries a real contact address, so company
discovery silently finds nothing without it. It's read from the environment rather than
committed, so a public repo never publishes your address.

Both are skipped gracefully if unset.

### 5. GitHub Actions (the scheduler)

Push this repo to GitHub, then add each `.env` value under
**Settings → Secrets and variables → Actions**: `DATABASE_URL`, `NTFY_TOPIC`,
`ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`, `GMAIL_APP_PASSWORD`, `ADZUNA_APP_ID`,
`ADZUNA_APP_KEY`, `USAJOBS_API_KEY`, `USAJOBS_EMAIL`, `SEC_CONTACT_EMAIL`.

The workflow polls every 30 minutes and runs SEC discovery weekly. Trigger it by hand
from the Actions tab (**Run workflow**) to test.

> Use a **public** repo if you can — Actions minutes are unlimited for public repos,
> while private repos get 2,000 min/month, which a 30-minute schedule would exhaust.
> No secrets live in the code; they're all in GitHub Secrets.

### 6. Render (the dashboard)

New → Web Service → connect this repo. `render.yaml` supplies the build and start
commands. Set `DATABASE_URL` in the Render dashboard to the same Supabase string.

Free instances sleep when idle, so the first request after a while takes ~30–60s.

---

## Usage

Create filters on the **Filters** page. A filter matches when *any* selected sector
or keyword hits, exclusions always veto, and location/remote constraints must hold.
A filter with no sectors and no keywords matches every internship — useful as a
catch-all. Each match alerts once per channel, ever (`alerts_sent` enforces it).

### Commands

```bash
python3 poller/main.py --verbose              # normal poll
python3 poller/main.py --board-slice 500      # poll more boards this run
python3 poller/main.py --discovery            # + SEC company discovery (slow)
python3 poller/main.py --skip-alerts          # store without notifying
python3 poller/retag.py --dry-run             # preview retag after editing sectors.py
python3 poller/dedupe.py --dry-run            # preview merging duplicate postings
python3 tests/test_sectors.py                 # tagging regression tests
python3 tests/test_alerts.py                  # alert delivery (unicode titles)
python3 tests/test_dedupe.py                  # cross-source dedupe identity
```

### Tuning sectors

Edit the keyword lists in `shared/sectors.py`, run `python3 tests/test_sectors.py`,
then `python3 poller/retag.py` to update already-stored postings.

Two gotchas encoded in the tests, both found by real mis-tagging:
- Keywords anchor **strictly at the start** of a word, or `arch intern` matches
  "rese*arch intern*" and tags every research role as computer architecture.
- The **trailing** edge stays loose so `firmware engineer` also matches "firmware
  engineer*ing*".

---

## Notes

- **Runtime budget.** The watchlist is >1,500 companies; polling all of them every
  run would take far too long (a full sweep is ~15 min and ~136k raw postings).
  Boards are polled in a rotating slice ordered by least-recently-checked
  (`--board-slice`, default 250), so the full list is covered over a few runs.
  Tracker feeds are cheap and run every time.
- **Crash safety.** Board results are committed every 50 companies rather than once
  at the end, so a crash or runner timeout part-way through a long sweep keeps the
  work already done instead of discarding it.
- **First run.** Seed with `--skip-alerts` before enabling alerts, or every existing
  posting counts as new and you get thousands of notifications at once.
- **Dedupe.** Postings are keyed on the canonical URL *alone* — lowercased, with
  tracking params and any `/application` suffix stripped. Sources disagree on
  everything else: aggregators truncate titles ("Product Analyst Intern" vs
  "…(Spring/Summer 2026)") and record company names differently ("Aquatic" vs
  "Aquatic Capital Management"), so including either field re-creates duplicates.
  Run `python3 poller/dedupe.py` to merge rows stored under an older key.
- **Workday dates.** Workday returns relative text ("Posted 30+ Days Ago") rather
  than a timestamp, so `first_seen_at` — not `posted_at` — drives new-posting alerts.
- **Portable storage.** List-valued columns are stored as `|a|b|` strings rather than
  Postgres arrays, so the identical schema runs on SQLite locally and Postgres in
  production.
