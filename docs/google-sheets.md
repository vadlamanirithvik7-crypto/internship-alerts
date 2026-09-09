# Live application spreadsheet ($0)

The app owns application stages and notes. Google Sheets receives one row per
posting ID whenever progress changes. Edit stages in the app; edits to the mirrored
columns in Sheets are overwritten by the next app update. Additional personal
columns after the hidden sync-version column K are left alone.

1. Create a private Google Sheet with an `Applications` tab. Row 1 must contain:
   Posting ID, Company, Role, Location, Term, Status, Applied at (UTC),
   Updated at (UTC), Application URL, Notes, Sync version.
2. Create an owner-controlled Apps Script project with `scripts/google-sheets.gs`.
   Set Script Properties `SHEET_ID` and `SYNC_TOKEN` (a randomly generated secret
   of at least 32 bytes). Do not put the token in source control, a URL, or chat.
3. Deploy a Web app, executing as the owner, access Anyone. Approve the requested
   Google Sheets permission yourself. The Sheet itself stays private. The webhook
   checks the secret before opening it and exposes no application data through GET.
4. In Render and GitHub Actions secrets, set `GOOGLE_SHEETS_WEBHOOK_URL` to the
   resulting `https://script.google.com/macros/s/.../exec` URL and
   `GOOGLE_SHEETS_SYNC_TOKEN` to the same secret. In Render also set
   `GOOGLE_SHEET_URL` to the private Sheet's `https://docs.google.com/spreadsheets/d/.../edit` URL.
5. Redeploy. The Applications page links to the Sheet and reports queued changes.
   Each application update commits to PostgreSQL before the background sync.
   Unacknowledged changes retry on visits to Applications and scheduled worker
   runs, even while the free web server sleeps.

The receiver locks concurrent writes, upserts IDs, and rejects older versions.
Retries do not create duplicate rows. The app only acknowledges the version sent,
so a concurrent newer edit remains queued. The Excel download provides a separate
database-backed copy. Google Apps Script's free quotas apply; failures remain
queued, with no paid fallback or automatic upgrade.
