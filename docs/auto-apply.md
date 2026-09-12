# Local auto apply

Radar's `/autopilot` page controls a dedicated headless Chromium worker on the owner's Mac. It does not attach to Chrome, request accessibility control, or move the mouse. The process runs at lower CPU priority, handles one application at a time, and uses local Ollama inference with two CPU threads and GPU inference disabled. No paid browser, model API, or hosting service is required. The Mac must be awake and connected; sleep suspends work.

## Setup and controls

1. Upload software and hardware PDFs under Resumes & details, and save correct contact information. The email must be a valid address, without the comma from the original chat typo.
2. In Auto apply, select the PDF for each of software, hardware, and embedded/firmware. The same PDF can serve multiple categories. Save explicit question = answer lines for work authorization, sponsorship, education, graduation, availability, and other employer questions.
3. Download the Mac connection and double-click Connect Radar Worker.command in Desktop/Internship Radar Controls. The file is stored privately under `~/Library/Application Support/Internship Radar Worker/connection.json`. Replacing it revokes the old token and stops automation.
4. Choose Start / Resume on the site and double-click Start Radar Auto Apply.command. Future eligible listings enter the queue. Historical listings are not submitted automatically. New listings discovered while paused are included on resumption.
5. Pause keeps the queue and closes in-progress preparation. Stop additionally exits the local worker. To restart after Stop, use both the website's Start / Resume and the local Start launcher. Local Stop sends SIGTERM; the worker closes its browser and reports Stopped to the website when connected.

Controls are checked every two seconds during browser work and immediately before a one-use submission permit. A stop cannot revoke an HTTP request already sent. An interrupted submission without a receipt is Uncertain and never automatically retried. Check employer confirmation and record the application manually.

## Supported behavior and limits

The first adapter supports single-page employer forms hosted by Greenhouse, Lever, Ashby, and equivalent supported frames (including CareerPuck embeds). Workday and other login/multi-step flows commonly require manual completion; inclusion in the host allowlist does not imply full Workday automation. Every task must have an exact employer job URL, a matching rendered role title, and a confirmed resume upload control. General careers links remain Waiting link.

The local model only selects an equivalent existing saved-answer key. It cannot invent applicant facts, generate a signature, authorize consent, or answer an assessment. Sensitive answers require an exact question match. Required unknown questions, unsupported inputs, CAPTCHA, account login, or eligibility restrictions produce Needs input. The owner can save the missing answers and retry unsubmitted tasks. A positive employer receipt is required to mark Applied and enqueue the existing idempotent Google Sheets sync.

Queue identity uses normalized company and role, intentionally suppressing multi-location and multi-source copies. Resumes and applicant answers are pinned per task. Already applied or dismissed roles are checked again before submission. Separate new tables preserve historical retired-queue records and inherit PostgreSQL RLS protection at creation.

The saved preference `Skip applications that ask about US citizenship = Yes` cancels a task before entering personal data if a form asks about US citizenship, including optional questions and radio groups. Explicit citizenship/permanent-residency restrictions also cancel. The task records the reason, remains in history, and is never marked Applied or sent to the application spreadsheet. The listing remains available for manual review. Answer variants concerning authorization, sponsorship, visa status, or availability still require saved facts; this preference does not infer any of them.

Private resumes and answers are sent only over the authenticated worker connection and to the selected employer's supported application forms. No applicant data, connection tokens, or page contents are printed in worker logs. Public third-party page text is untrusted; model output is restricted to known saved keys. The worker blocks private-network browser requests and unsupported form POST destinations.

## Validation

`python -m pytest -q` covers role-based queue creation, source deduplication, unknown required questions, stop/pause and permit gating, expired submission leases, idempotent receipts, authentication, demo isolation, and synthetic browser submission/confirmation. Browser tests intercept all employer traffic; they do not send real applications.
