# Hosted rollout verification

Verified September 8, 2026 (America/Chicago), using application commit `fce87278f9057090e635da3d3286596b3a234494`.

- Render Free deployed successfully. The private workspace and separate public demo use the same free web service.
- Anonymous `/` and `/applications.xlsx` return 401; anonymous `/demo/` returns 200. Authenticated Applications returns 200 with the private Google Sheet link.
- All ten application tables have PostgreSQL row-level security enabled. PostgreSQL CI checks that anonymous and authenticated Data API roles lack table privileges.
- A clearly fictional test role was marked Applied twice and then Interview through the hosted HTTP endpoints. Its original application time stayed unchanged. The Excel export and native Google Sheet each contained one row in the final Interview stage. The hosted queue recorded the matching Google Sheets acknowledgement.
- The test role and sync record were removed from PostgreSQL, the Excel backup regenerated, and the exact test row removed from Sheets. No real application was marked or deleted.
- Both GitHub CI runs passed for this commit, including the PostgreSQL migration/restart test. Local tests: 87 passed, one PostgreSQL-only test skipped. CI supplies a disposable PostgreSQL 16 service for that test.
- Real email and ntfy delivery receipts were recorded on September 8. A receipt means the configured transport accepted delivery, not that the user opened or read it.

The app uses free local ONNX embeddings, Render Free, Supabase Free, public-repository GitHub Actions, and the owner's Google Apps Script/Sheets. Free services can sleep, delay runs, enforce quotas, or change terms. Polling and public source freshness do not provide an instant or exhaustive listing guarantee.
