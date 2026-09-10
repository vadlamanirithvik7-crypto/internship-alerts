# Selected applications and Gmail updates

Use the private website on your phone. There is no App Store download.

1. Open **Resumes & details** (also available from Profile or Applications).
2. Save your name and the exact application email. Upload up to three PDFs from Files on your phone, each at most 2 MB and ten pages. These are the original attachments; existing semantic matching profiles only contain extracted text and are separate.
3. Open a role, tap **Apply with my resume**, and select a PDF or upload one into an empty slot. The final button authorizes this particular application.
4. The free scheduled worker prepares supported forms. Its questions appear in **Apply queue**. Supply the employer's required answers; optional ones can be skipped. Review-every-form mode is available in settings. No work authorization, sponsorship, demographic, signature or custom essay answer is guessed.
5. Only an employer confirmation, a matched receipt email, or your explicit verification marks Applied. The role leaves default discovery, stops new-role alerts, and enters the existing durable Google Sheets sync. It remains in application history.
6. **Email updates** displays receipts, interviews, rejections and next steps after Gmail is connected. Ambiguous matches require choosing an application. No email replies are sent automatically.

## Actual support boundary

Automatic browser submission currently supports **direct Lever and Greenhouse application URLs with identifiable native forms and native HTML controls**. Custom React dropdowns, required additional attachments, Workday/account-based flows, unsupported ATS sites, CAPTCHA and multi-step forms require completion on the employer's site. The app states this explicitly and keeps the chosen PDF available. A worker browser session cannot be transferred to a phone browser. There is no CAPTCHA bypass, employer API key, or claim of universal one-click applications.

Current Lever form markup was inspected read-only, including its JavaScript Submit button and native-backed Select2 school picker. Browser behavior is tested against intercepted fixtures in Chromium. These fixtures cover required/custom answers, PDF attachment, conditional fields, review mode, confirmation and uncertain outcomes. They do not prove that any particular live employer currently supports automation. ATS markup changes and must fail visibly to a handoff.

## State and consistency

`queued → running → needs_info / needs_review / needs_action / submitting → submitted / uncertain`

Each posting and canonical supported employer application URL has at most one durable task. Repeated button taps return that task. The selected attachment and profile are snapshotted; archiving a PDF does not change existing applications. A resume can be changed before submission when the worker is idle, clearing earlier custom answers. Cancel is unavailable after submission starts.

Workers claim tasks with conditional updates and a random token. Stale preparation can be retried after fifteen minutes. The `submitting` state is committed **before clicking Submit**. A lost browser or unverified response becomes `uncertain`, never an automatic retry. This prevents blind repeated submissions but does not claim atomicity across an employer's site and this database. The confirmation, application status, workbook and Google Sheets outbox are committed together. Manual stage changes stop preparation; changes during submission are rejected until its result is known.

The isolated browser has no dashboard credentials, mailbox token, cookies or persistent profile. Only HTTPS/public destinations are allowed; direct ATS URLs are validated before any filling. Resumes, screenshots, form bodies and traces are not uploaded to GitHub artifacts or printed in logs. Application data stays in the existing private database, except the selected information intentionally sent to the employer and application rows mirrored to the owner's Sheet. New tables are covered by existing startup RLS and client-role privilege revocation.

## Free worker deployment

`.github/workflows/apply.yml` runs on a standard Ubuntu hosted runner only while the repository is public. It requests a run every five minutes, separately from the internship scanner. GitHub scheduling can be delayed, and Render can sleep. Three selected tasks are processed per run, with a twelve-minute run limit. It uses the repository's existing `DATABASE_URL`, `GOOGLE_SHEETS_WEBHOOK_URL`, and `GOOGLE_SHEETS_SYNC_TOKEN` secrets. It does not require a new paid service or API. Never enable a paid runner or increase a hosting plan.

The queue shows the last worker heartbeat; it does not imply instant processing. Supported forms are never attempted until the owner selects that role and resume.

## Connect the correct Gmail account

The Sheet owner's account and application mailbox may differ. Outgoing Gmail alert credentials do not establish inbox monitoring.

In **Resumes & details → Connect your Gmail**, download the private setup ZIP. It includes a one-time generated connection token, code, explicit manifest scopes and instructions. Sign in to Google Apps Script as the exact application email, create a private project, paste Code.gs and appsscript.json, and run `configureConnection`. Google must authorize these new scopes. Do not deploy this script as a public web app or share the generated code. The checked-in source has no token.

The script uses the advanced Gmail service with **gmail.readonly**, external HTTPS requests, and an installable five-minute trigger. The app checks the mailbox identity before accepting events. The revocable token grants only mailbox configuration and event ingestion; it cannot retrieve PDFs or application answers. Downloading setup again rotates it. Disconnecting in the app immediately revokes ingestion; running `disconnectRadar` removes the trigger. Remove the script's permission in Google account settings to revoke Google's read access too.

The script rotates batches of twelve company names across up to 500 recent tracked applications, pages thirty messages at a time, and searches the previous thirty days excluding sent, draft, spam and trash. It does not modify the inbox or send mail. Only related subject, sender, timestamp and a bounded excerpt are retained. Strong unique company-and-role matches can advance application stages; ambiguous or generic updates remain for review. Old emails cannot overwrite a newer manual status or a terminal outcome. Matching is deterministic, not a paid AI service, and is not guaranteed to recognize every wording or company alias. Open the original email for complete next steps.

## Validation

Run `pip install -r requirements-worker.txt pytest httpx`, `python -m playwright install chromium`, and `python -m pytest -q`. Chromium tests intercept all employer requests and send no real applications. CI also verifies PostgreSQL migrations, unchanged restart behavior and RLS on every table.
