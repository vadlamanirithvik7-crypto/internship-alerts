# Internship Radar

A phone-friendly internship discovery app with resume-aware semantic matching, evidence-backed comparisons, saved applications, and an observable alert pipeline. Built with Python, FastAPI, Jinja, SQLAlchemy, PostgreSQL/SQLite, and locally executed ONNX embeddings.

**Budget: $0.** No paid inference API, required API credits, paid database, or paid hosting plan. Optional services must remain within their free allowances. The public demo is separate from private data and notification credentials.

## Hosted app

- Private workspace: https://internship-alerts-1412.onrender.com/
- Public recruiter demo: https://internship-alerts-1412.onrender.com/demo/

The live workspace runs on Render Free with Supabase PostgreSQL and password protection. The laptop is not needed. The public demo uses isolated fictional data. Free-instance cold starts may take 50 seconds or more.

**Live Google Sheets sync verified September 8, 2026.** Marking Applied from the hosted app writes the application to the owner's private Sheet. Repeated updates preserve the original application date and update one existing row; the Excel backup stays available in Applications. Change stages and notes in the app: synchronization is one-way from the app to Sheets.

The live feed and alerts target explicitly confirmed US summer 2027 internships and co-ops. GitHub Actions requests polling every five minutes after this workflow is on the default branch; queued runs and source update delays can increase that interval. Email and ntfy transport acknowledgements have been recorded for real matching roles. Those acknowledgements do not establish that a person read the notification.

Render service: `srv-dag8mauk1f9s7388sar0`. Credentials belong in the service's Environment screen and Actions secrets; never put them in this document, chat, or source control. See [rollout verification](docs/rollout-verification.md) and [Google Sheets setup](docs/google-sheets.md).

## Run the expo demo

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-ai.txt
.venv/bin/python scripts/demo.py --warm
DEMO_MODE=1 .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. On a phone on the same Wi-Fi, use your computer's LAN IP and port 8000. For access away from the computer, deploy the single **free** service in `render.yaml` and share its `/demo/` URL. `render-demo.yaml` remains an alternative for demo-only hosting; do not deploy both if you want to reserve free instance hours. Render's free service can sleep, so open the URL before your conversation. The demo service worker saves the three profiles and job pages for offline use after its first successful load. Wait for “Demo saved for offline use.” Arbitrary searches not previously cached show an offline page with links to the saved demo. HTTPS is required for service workers outside localhost.

The fictional dataset includes 18 roles and three sample profiles. Scores are produced by the actual embedding model. Job/company/source observations are clearly labeled as sample data. Saves, stages, and notes persist only in the visitor's browser; the public demo cannot modify production data. Do not present fictional employers or the synthetic evaluation as real usage.

Phone walkthrough (about one minute):
1. Start with **Software engineering**, open the top role, and point out the resume/job excerpts.
2. Switch to **Embedded systems** to demonstrate personalized ranking.
3. Save a role, change its application stage, and open **Saved**.
4. Open **Alerts** or **Behind the build** to discuss retries, deduplication, and deployment.

## Fully online, $0 deployment

Use **one** Render Free web service (`render.yaml`), the existing external Supabase Free PostgreSQL database, and the existing GitHub Actions scheduled worker in this public repository. No paid instance, Render database, disk, custom domain, or inference subscription is needed. Confirm both hosting/database accounts remain on Free and do not enable automatic paid upgrades.

1. Deploy the tested branch/merged main with `render.yaml`. Set `DATABASE_URL` to the **same** database used by the GitHub Actions secret. Set a strong `ADMIN_PASSWORD` in Render; never put credentials in a URL or the repository.
2. `/` is the protected working app: real roles, resume uploads, cloud-saved applications, company priorities, alert filters, and delivery history. `/demo/` is the public recruiter walkthrough using isolated fictional data. It cannot read the live database or change alert settings.
3. The build downloads the free local embedding model and warms the demo. Live resume/job vectors are cached in PostgreSQL. Alert credentials belong only in Actions, not in the public demo.
4. Merge the tested workflow onto the default branch to activate the new scheduled poller. Existing `DATABASE_URL`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`, `GMAIL_APP_PASSWORD`, and `NTFY_TOPIC` secrets supply storage and notification delivery. Optional `NTFY_TOKEN` and `NTFY_BASE` support an authenticated/custom ntfy service.
5. Open the protected `/filters` page, choose sectors/keywords/locations and channels, and create an active filter. Subscribe to the configured topic in the ntfy phone app to receive pushes; email goes to the configured recipient. The UI shows queued/completed deliveries. Filters and resume ranking are separate controls.
6. Verify a scheduled poll finishes, live roles appear in the browser, and a naturally matching new role produces a recorded delivery received on the intended channel. Fixture tests do not establish live transport delivery.

Render Free sleeps after 15 idle minutes and shares 750 instance hours per workspace/month. Alerts run in Actions independently of the sleeping website; schedules can be delayed. Free Supabase projects can pause after low activity. A Cloudflare quick tunnel is only a temporary development preview and still needs the laptop running. Nothing here promises an always-awake service or instant alerts.

## Private workspace

```bash
cp .env.example .env
# Fill DATABASE_URL and ADMIN_PASSWORD; do not commit .env.
.venv/bin/uvicorn backend.main:app --env-file .env --host 0.0.0.0 --port 8000
```

Without `ADMIN_PASSWORD`, live-data pages accept only localhost requests. With a password, browsers open a regular sign-in page using the configured Radar password. An HttpOnly, SameSite=Strict cookie keeps that browser signed in for seven days; changing ADMIN_PASSWORD invalidates existing sessions. HTTP Basic authentication remains supported for API clients (any username; configured password). Use HTTPS on deployment. The public demo always uses `demo.db`, even if `DATABASE_URL` is set. Resume upload accepts text-based PDF or UTF-8 text, up to 2 MB / ten PDF pages; scan-only PDFs require pasted text. Resumes and descriptions stay in your database. The local embedding model does not transmit them to an inference service.

Profiles support preferences, location restrictions, term, exclusions, and remote-only filtering. Applications support interested, applied, interview, offer, and rejected stages, plus notes. Job closure is separate from application status. Priority companies are polled in addition to the rotating board slice.

On a role's page, **Mark applied** records the first application time and marks it **Applied — done**. Completed applications leave the default discovery feed and stop producing new alerts. The Applications page retains their stages and notes. Each progress update writes an Excel workbook and a Google Sheets sync record to PostgreSQL in the same transaction. The free [Google Sheets bridge](docs/google-sheets.md) updates the owner's private Sheet in the background, with durable retries and protection against duplicate or out-of-order updates. Its pending count is visible in Applications. **Download application spreadsheet** exports a separate persistent Excel backup with company, role, location, term, stage, dates, URL, and notes. The public demo cannot sync or export private applications.

The live feed and alert worker require explicit evidence of a **United States work location**, an **internship or co-op**, and **summer 2027**. Unknown remote regions, unsupported seasons, and graduation-year-only references are excluded. This conservative scope may omit a relevant role until its source supplies enough evidence. Existing historical rows remain stored separately from the targeted discovery feed.

## Manual applications and eligibility

Open a role and tap **Open employer application** to apply yourself. Radar never fills or submits the application. After submitting, use **Mark applied** to remove the role from discovery and update the live Google Sheet. Saved PDFs remain available for download. The former automatic submission worker and queue are removed; historical records remain stored.

The three role filters use actual job titles: software engineering, embedded/firmware, and electrical/hardware/chip design. Broad skill or employer-description mentions no longer make unrelated roles qualify. Listed citizenship/permanent-residency requirements and graduate-only roles are excluded from discovery and alerts; bachelor's/master's alternatives remain eligible. Missing or incomplete requirements cannot establish individual eligibility—check employer requirements and work authorization before applying. **Not interested** hides a card persistently and stops its alerts; restore it from Applications → Not interested by changing its stage to New.

Optional owner-authorized Gmail tracking continues to show receipts, interviews, rejections and next steps after connection. See [manual application and email setup](docs/application-assistant.md).

## AI matching and evidence

`shared/matching.py` chunks retained descriptions and resumes, runs `sentence-transformers/all-MiniLM-L6-v2` through FastEmbed/ONNX on CPU, and combines semantic similarity, exact tokens, and freshness. Model/text/version hashes cache vectors in the database. Profile constraints apply before ranking. `C++`, `C#`, `.NET`, and other exact terms are preserved. Keyword and weighted baselines are selectable; unavailable inference falls back visibly to keywords. The first model download and uncached large corpora are slower than cached requests.

The default explanation is extractive: each skill comparison shows exact source passages. Optional **free, locally hosted Ollama** generation can be enabled with `OLLAMA_URL=http://localhost:11434` and `OLLAMA_MODEL=llama3.2:3b`. Ollama is not required or bundled. Generated quotes must be nonempty substrings of the correct source document; invalid output or timeouts use source excerpts instead. Evidence grounding verifies quotes, not every possible semantic interpretation. Relevance is not a probability of being hired, and “not demonstrated” does not mean a person lacks a skill.

Run `python evaluation/evaluate.py evaluation/demo_dataset.json`. See [evaluation/README.md](evaluation/README.md) for metrics, real-posting collection, labeling, and dev/test separation. The supplied benchmark is synthetic and must not be cited as production accuracy.

## Pipeline

```text
GitHub Actions schedule → tracker/search feeds → normalization and retained descriptions
                      → priority + rotating ATS boards (8 worker threads)
                      → dedupe, tagging, availability, incremental commits
                      → PostgreSQL / SQLite ← FastAPI phone dashboard
                      → durable delivery outbox → email digest / ntfy
                      → source observations + run history → health view
```

Supported boards: Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, and Recruitee. Tracker feeds include Simplify-compatible JSON, markdown trackers, Hacker News, and optional authenticated Reddit. Public career-site adapters also query Amazon and Microsoft's current careers endpoints. Search sources include optional Adzuna and USAJOBS credentials, Arbeitnow, and RemoteOK. Public endpoints can change or disappear; source health makes errors visible. Recruitee has announced a Careers Site API authentication change for February 2027; that adapter may need company-issued credentials then. No credentials are bypassed.

Companies grow through observed ATS URLs, SEC EDGAR sector discovery, and live board probes. SEC requires a real `SEC_CONTACT_EMAIL` in the User-Agent; set it in secrets, not source code. A watchlist company is not necessarily resolved or polled directly.

`python poller/main.py --board-slice 100 --resolve-slice 40 --verbose` runs ingestion and alerts. The workflow requests a run every five minutes, but GitHub can delay or queue it. The worker checks current Summer2027 trackers, polls manually prioritized boards each run, gives relevant boards another check after 15 minutes, and rotates through the remaining watchlist. Feed fingerprints skip unchanged ingestion. Frequent scheduled runs skip slow unknown-board resolution; the weekly discovery run handles it. Alerts are delivered after feed ingestion and after board batches with new roles, before the full sweep ends. This reduces discovery delay without promising instant or exhaustive coverage.

`--discovery` adds SEC discovery. `--skip-search` skips broad keyword sources. HN, Reddit, Microsoft, and Amazon are fetched at most once per six hours. `--board-slice 0` disables all board polling, including priority companies.

### Reliability semantics

- **Retries:** new candidates are discovered from a bounded recent window in the database, then persisted in a durable outbox. Failed deliveries remain pending beyond that window. New filters can match yesterday's postings.
- **Delivery:** successful receipts are unique per posting/filter/channel. SMTP and ntfy do not provide a transaction shared with the database; a crash after delivery but before recording a receipt can repeat a message. This is at-least-once delivery, not guaranteed exactly-once delivery.
- **Push batches:** only acknowledged posting IDs are recorded. Failed or oversized digests and remaining burst rows stay pending. Each delivery pass consolidates email into a digest grouped by filter; one poll can send more than one digest as additional boards finish.
- **Deduplication:** canonical URL identity merges shared URLs. Exact company/title/location soft keys suppress a Jobright-wrapper/direct-link duplicate notification; they never merge records or suppress two different direct requisitions. Different wrapper titles/locations can still evade this conservative heuristic.
- **Tagging:** descriptions are retained (up to 30,000 characters), and insert/retag use the same stored input. Legacy NULL descriptions preserve existing tags until text is harvested again.
- **Availability:** every sighting updates `last_seen_at`. Explicit tracker inactivity closes tracker-origin rows. Three complete successful direct-board scans missing a posting close direct-origin rows; failed, truncated, or skipped scans never count. Direct-board evidence takes precedence over aggregator inactivity. A fresh direct sighting reopens a role.
- **Isolation:** workers receive plain company snapshots instead of sharing ORM objects. Board progress commits every 50 companies. PostgreSQL advisory locks or a local file lock prevent overlapping pollers; CI also uses a concurrency group.
- **Health:** run/source observations record counts, status, timing, and incomplete boards. Deterioration and recovery can notify through ntfy; unchanged failures stay quiet. Unconfigured Reddit is reported as skipped. Interrupted runs are identified on the next start.

Use `--skip-alerts` for baseline ingestion: newly inserted baseline rows stay ineligible for alerts on later runs. For a temporary notification pause, pause the filters instead. Existing receipts and pending deliveries are preserved by upgrades.

## Database upgrade

Startup applies an additive, idempotent migration to old databases before creating new tables. It adds description/availability/tracking/priority columns and new outbox, profile, cache, and health tables; it does not drop existing data. SQLite upgrades are regression-tested, and CI also runs the migration against an isolated PostgreSQL 16 service.

Before upgrading a live database, take a provider backup and inspect the branch changes. To inspect tagging changes: `python poller/retag.py --dry-run`. Legacy rows without retained descriptions are skipped deliberately. `python poller/dedupe.py --dry-run` previews URL collisions; the mutating mode preserves receipt/outbox history, notes, and application progress.

## Alerts and deployment

Set GitHub repository secrets from `.env.example`: database URL, Gmail sender/recipient/app password, ntfy topic, and optional source credentials. GitHub supplies `GITHUB_TOKEN` automatically for Actions. `NTFY_TOKEN` and `NTFY_BASE` support an existing protected topic/account or your own server. A token alone does not make a public topic private; configure topic access separately. Do not purchase a plan for this project.

`render.yaml` is the private dashboard configuration. `render-demo.yaml` is the isolated free public demo. They must not share data. The app checks the database at `/healthz`. Test changes on the development branch before promoting the poller workflow or applying migrations to production.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The suite runs the original executable regression scripts as well as new assertions for delivery retry/receipts, closures, Workday caching, source adapters, migration, matching constraints, invalid citations, authentication, upload, and application updates. Standard tests use fixtures and mock external delivery; they do not send email or phone notifications. CI has no production credentials in its test job. Set `AI_ENABLED=0` for offline tests without model installation. Real-model demo warmup is a separate smoke check.
