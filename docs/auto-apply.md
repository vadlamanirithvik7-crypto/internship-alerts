# Local auto apply

Radar's `/autopilot` page controls a dedicated headless Chromium worker on the owner's Mac. It does not attach to Chrome, request accessibility control, or move the mouse. The process runs at lower CPU priority, handles one application at a time, and uses local Ollama inference with two CPU threads and GPU inference disabled. No paid browser, model API, or hosting service is required. The Mac must be awake and connected; sleep suspends work.

## Setup and controls

1. Upload software and hardware PDFs under Resumes & details, and save correct contact information. The email must be a valid address, without the comma from the original chat typo.
2. In Auto apply, select the PDF for each of software, hardware, and embedded/firmware. The same PDF can serve multiple categories. Save explicit question = answer lines for work authorization, sponsorship, education, graduation, availability, and other employer questions.
3. Download the Mac connection and double-click Connect Radar Worker.command in Desktop/Internship Radar Controls. The file is stored privately under `~/Library/Application Support/Internship Radar Worker/connection.json`. Replacing it revokes the old token and stops automation.
4. Choose Start / Resume on the site and double-click Start Radar Auto Apply.command. Future eligible listings enter the queue. Use **Queue existing internships** to add the historical backlog with your current saved resumes and answers. It preserves the automatic new-listing cutoff, skips closed/dismissed/applied/duplicate roles, and can be used before your first Start. Repeating it does not duplicate work. New listings discovered while paused are included on resumption, and newer discoveries take priority over historical work. The queue shows total counts by state and pages through every task.
5. Pause keeps the queue and closes in-progress preparation. Stop additionally exits the local worker. To restart after Stop, use both the website's Start / Resume and the local Start launcher. Local Stop sends SIGTERM; the worker closes its browser and reports Stopped to the website when connected.

Controls are checked every two seconds during browser work and immediately before a one-use submission permit. A stop cannot revoke an HTTP request already sent. An interrupted submission without a receipt is Uncertain and never automatically retried. Check employer confirmation and record the application manually.

## Live activity

The Auto apply page refreshes its live panel every three seconds without reloading your settings. It shows the current company and role, the application step, totals by state, the latest ten attempts, and the Mac's connection status. Worker heartbeats report opening, checking, filling, advancing, submitting, and awaiting confirmation. A heartbeat older than thirty seconds is displayed as disconnected; failed dashboard requests are explicitly marked stale and retry automatically. The owner-only activity endpoint never includes resume contents, saved answers, contact details, or worker credentials.

## Answering questions and reviewing failures

The **Questions waiting for you** section at the top of Auto apply displays employer questions directly, with text boxes or captured answer choices. **Save and continue** saves that application's answer snapshot and queues it without navigating away, even while the worker handles other jobs. The live refresh preserves typing and focus, and validation failures keep the draft in place. Up to twenty applications load initially; more appear as these are answered. Employer-site failures without questions are counted separately. Confirmed submissions are always shown explicitly, including zero.

Answers stay specific to this application unless the owner explicitly checks Remember for future applications. Submitted or uncertain applications cannot use this flow, and stale question forms are rejected. Saving does not start a paused or stopped worker. The separate application help page remains available for manual review and bookmarked links.

Browser errors, unrecognized controls, login requirements, and missing attachments show **Needs manual review**, with the recorded reason and a link to the employer site. These diagnostics are not presented as text questions. Existing string-based question records remain readable; newly captured questions can include structured dropdown choices. The normal role page also links to the relevant application help page.

## Supported behavior and limits

The worker attempts single-page and common multi-step employer forms, including custom employer portals. It follows recognized Apply, Apply manually, Next, Save and continue, and Review application controls for up to ten steps. Personal data can be sent only to the original employer URL's exact HTTPS origin or the supported ATS hosts (Greenhouse, Lever, Ashby, Workday, and CareerPuck). Other portal transitions, ambiguous controls, employer accounts, CAPTCHA, and unknown required answers need manual completion. This is a general fallback, not universal site compatibility; Workday's account requirement commonly stops automatic completion. Every task still requires an exact employer job URL, a matching rendered role title, a confirmed resume upload, and a final submission permit. General careers links remain Waiting link.

The local model only selects an equivalent existing saved-answer key. It cannot invent applicant facts, generate a signature, authorize consent, or answer an assessment. Sensitive answers require an exact question match. Required unknown questions, unsupported inputs, CAPTCHA, account login, or eligibility restrictions produce Needs input. The owner can save the missing answers and retry unsubmitted tasks. A positive employer receipt is required to mark Applied and enqueue the existing idempotent Google Sheets sync.

Queue identity uses normalized company and role, intentionally suppressing multi-location and multi-source copies. Resumes and applicant answers are pinned per task. Already applied or dismissed roles are checked again before submission. Separate new tables preserve historical retired-queue records and inherit PostgreSQL RLS protection at creation.

The saved preference `Skip applications that ask about US citizenship = Yes` cancels a task before entering personal data if a form asks about US citizenship, including optional questions and radio groups. Explicit citizenship/permanent-residency restrictions also cancel. The task records the reason, remains in history, and is never marked Applied or sent to the application spreadsheet. The listing remains available for manual review. Answer variants concerning authorization, sponsorship, visa status, or availability still require saved facts; this preference does not infer any of them.

Private resumes and answers are sent only over the authenticated worker connection and to the selected employer's supported application forms. No applicant data, connection tokens, or page contents are printed in worker logs. Public third-party page text is untrusted; model output is restricted to known saved keys. The worker blocks private-network browser requests and unsupported form POST destinations.

## Validation

`python -m pytest -q` covers role-based queue creation, source deduplication, unknown required questions, stop/pause and permit gating, expired submission leases, idempotent receipts, authentication, demo isolation, and synthetic browser submission/confirmation. Browser tests intercept all employer traffic; they do not send real applications.
