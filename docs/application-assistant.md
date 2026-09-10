# Manual applications and Gmail updates

Use the private website on your phone; no App Store download is needed.

1. Open a role and tap **Open employer application**. Complete and submit its form yourself.
2. If useful, download a saved PDF from **Resumes & details** and attach it on the employer site.
3. After submitting, return to Radar and tap **Mark applied**. The role leaves Discover and its stage, date and notes sync to your existing Google Sheet.
4. Use **Not interested** on a feed card to hide it and stop alerts for it. Applications → Not interested retains it; choose New to restore it.
5. **Email updates** shows related receipts, interviews, rejections and next steps once Gmail is connected. Ambiguous matches need your review; the app sends no replies.

Automatic submission, the scheduled browser worker and its queue controls have been removed. Old queue URLs lead to Applications, old submission requests return 410, and startup cancels unsubmitted tasks. Historical tasks whose submission had started remain uncertain until confirmation. Existing PDFs and application history are retained.

Filters cover US summer 2027 internships/co-ops in software, embedded/firmware, and electrical/hardware/chip design. Role identity comes from titles. Listed citizenship/green-card requirements and graduate-only qualifications are excluded; bachelor's alternatives and optional advanced degrees are allowed. Requirements absent from the source cannot be verified, so review work authorization and qualifications on the employer site.

## Connect the correct Gmail account

The Sheet owner's account and application mailbox may differ. Outgoing Gmail alert credentials do not establish inbox monitoring.

In **Resumes & details → Connect your Gmail**, download the private setup ZIP. It includes a one-time generated connection token, code, explicit manifest scopes and instructions. Sign in to Google Apps Script as the exact application email, create a private project, paste Code.gs and appsscript.json, and run `configureConnection`. Google must authorize these new scopes. Do not deploy this script as a public web app or share the generated code. The checked-in source has no token.

The script uses the advanced Gmail service with **gmail.readonly**, external HTTPS requests, and an installable five-minute trigger. The app checks the mailbox identity before accepting events. The revocable token grants only mailbox configuration and event ingestion; it cannot retrieve PDFs or application answers. Downloading setup again rotates it. Disconnecting in the app immediately revokes ingestion; running `disconnectRadar` removes the trigger. Remove the script's permission in Google account settings to revoke Google's read access too.

The script rotates batches of twelve company names across up to 500 recent tracked applications, pages thirty messages at a time, and searches the previous thirty days excluding sent, draft, spam and trash. It does not modify the inbox or send mail. Only related subject, sender, timestamp and a bounded excerpt are retained. Strong unique company-and-role matches can advance application stages; ambiguous or generic updates remain for review. Old emails cannot overwrite a newer manual status or a terminal outcome. Matching is deterministic, not a paid AI service, and is not guaranteed to recognize every wording or company alias. Open the original email for complete next steps.

## Validation

Run `pip install -r requirements-worker.txt pytest httpx`, `python -m playwright install chromium`, and `python -m pytest -q`. Chromium tests intercept all employer requests and send no real applications. CI also verifies PostgreSQL migrations, unchanged restart behavior and RLS on every table.
