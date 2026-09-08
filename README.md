# Internship Radar

A phone-friendly internship discovery app with resume-aware semantic matching, evidence-backed comparisons, saved applications, and an observable alert pipeline. Built with Python, FastAPI, Jinja, SQLAlchemy, PostgreSQL/SQLite, and locally executed ONNX embeddings.

**Budget: $0.** No paid inference API, required API credits, paid database, or paid hosting plan. Optional services must remain within their free allowances. The public demo is separate from private data and notification credentials.

## Run the expo demo

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-ai.txt
.venv/bin/python scripts/demo.py --warm
DEMO_MODE=1 .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. On a phone on the same Wi-Fi, use your computer's LAN IP and port 8000. For access away from the computer, deploy the separate **free** service in `render-demo.yaml`; do not attach a production database or alert credentials. Render's free service can sleep, so open the URL before your conversation. The demo service worker saves the three profiles and job pages for offline use after its first successful load. Wait for “Demo saved for offline use.” Arbitrary searches not previously cached show an offline page with links to the saved demo. HTTPS is required for service workers outside localhost.

The fictional dataset includes 18 roles and three sample profiles. Scores are produced by the actual embedding model. Job/company/source observations are clearly labeled as sample data. Saves, stages, and notes persist only in the visitor's browser; the public demo cannot modify production data. Do not present fictional employers or the synthetic evaluation as real usage.

Phone walkthrough (about one minute):
1. Start with **Software engineering**, open the top role, and point out the resume/job excerpts.
2. Switch to **Embedded systems** to demonstrate personalized ranking.
3. Save a role, change its application stage, and open **Saved**.
4. Open **Health** or **Behind the build** to discuss retries, deduplication, and deployment.

## Private workspace

```bash
cp .env.example .env
# Fill DATABASE_URL and ADMIN_PASSWORD; do not commit .env.
.venv/bin/uvicorn backend.main:app --env-file .env --host 0.0.0.0 --port 8000
```

Without `ADMIN_PASSWORD`, live-data pages accept only localhost requests. With a password, HTTP Basic authentication protects the workspace (any username; configured password). Use HTTPS on deployment. The public demo always uses `demo.db`, even if `DATABASE_URL` is set. Resume upload accepts text-based PDF or UTF-8 text, up to 2 MB / ten PDF pages; scan-only PDFs require pasted text. Resumes and descriptions stay in your database. The local embedding model does not transmit them to an inference service.

Profiles support preferences, location restrictions, term, exclusions, and remote-only filtering. Applications support interested, applied, interview, offer, and rejected stages, plus notes. Job closure is separate from application status. Priority companies are polled in addition to the rotating board slice.

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

`python poller/main.py --board-slice 250 --resolve-slice 40 --verbose` runs ingestion and alerts. `--discovery` adds SEC discovery. `--skip-search` skips broad keyword sources. HN, Reddit, Microsoft, and Amazon are fetched at most once per six hours. `--board-slice 0` disables all board polling, including priority companies.

### Reliability semantics

- **Retries:** new candidates are discovered from a bounded recent window in the database, then persisted in a durable outbox. Failed deliveries remain pending beyond that window. New filters can match yesterday's postings.
- **Delivery:** successful receipts are unique per posting/filter/channel. SMTP and ntfy do not provide a transaction shared with the database; a crash after delivery but before recording a receipt can repeat a message. This is at-least-once delivery, not guaranteed exactly-once delivery.
- **Push batches:** only acknowledged posting IDs are recorded. Failed or oversized digests and remaining burst rows stay pending. Email is consolidated into one digest per run, grouped by filter.
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
