# ACMA RRL daily sync — GitHub Actions + Supabase

Supersedes the Apps Script/Google Drive version — that one's a dead end (see prior chat: Apps Script's `DriveApp`, `UrlFetchApp`, and almost certainly `Utilities.unzip` all cap around 50MB, and the real file is ~70MB and growing). Don't run both.

## What this does

A GitHub Actions job runs on a schedule, downloads `spectra_rrl.zip`, unzips it, and loads every CSV it finds into its own table in your Supabase Postgres database under an `acma_rrl` schema — full replace each run, wrapped in a transaction per table so a bad table doesn't corrupt a good one. A `acma_rrl._sync_log` table records every run: status, duration, row counts, anything that failed.

I still don't know the real table/column names inside the zip — same as before, every CSV gets a table named after its own filename with all-TEXT columns, and the first run's `_sync_log` entry will tell us exactly what showed up.

## 1. Get your Supabase connection string

1. In your Supabase project: **Project Settings → Database**.
2. Under **Connection string**, copy the **URI** — use the **Session pooler** variant if offered (more reliable for short-lived connections like a CI job than the direct connection).
3. It'll look like `postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres` — replace `[YOUR-PASSWORD]` with your actual database password.

## 2. Add it as a GitHub secret

1. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `SUPABASE_DB_URL`
3. Value: the full connection string from step 1.

## 3. Add the files to your repo

Commit these at the paths shown (matters — the workflow references `scripts/sync_acma_rrl.py` by that exact path):
```
.github/workflows/acma-rrl-sync.yml
scripts/sync_acma_rrl.py
```

## 4. Test it now — don't wait for tonight

1. **Actions** tab in your repo → **ACMA RRL daily sync** (left sidebar) → **Run workflow** button → **Run workflow**.
2. Click into the running job to watch the log live.
3. Check the outcome in Supabase: **Table Editor** → `acma_rrl` schema → you should see one table per CSV, plus `_sync_log`.
4. Query the log directly in the **SQL Editor**:
   ```sql
   select * from acma_rrl._sync_log order by run_at desc limit 5;
   ```

## 5. What to watch for on that first run

| Symptom | Likely cause |
|---|---|
| Job fails at `download_zip` | Same source-risk note as before — `web.acma.gov.au` was already past its stated retirement date when we found this URL. Check the log's HTTP status. |
| `status = PARTIAL` in `_sync_log` | Some CSVs loaded fine, one or more didn't — check `failed_tables` in that log row for the specific error (most likely a ragged row count vs. header, or an encoding issue the utf-8-sig/cp1252 fallback didn't catch). Send me that error text. |
| Tables don't match what you expected | Query `select table, rows, columns from acma_rrl._sync_log, jsonb_to_recordset(tables) as x(table text, rows int, columns jsonb) order by run_at desc limit 1;` — or just browse the Table Editor — to see the real structure ACMA actually shipped. |
| Nothing in the Actions tab at all | Scheduled workflows only run on the **default branch**, and GitHub auto-disables scheduled workflows after **60 days with no commits** to the repo — a new commit re-enables it. |

## 6. Two GitHub Actions quirks worth knowing

- **Scheduled runs aren't exact.** GitHub documents that cron-triggered workflows can be delayed, sometimes by tens of minutes, during periods of high platform load. Don't rely on the 11pm trigger firing at 23:00:00 sharp.
- **Failure alerting is already built in, free.** GitHub emails repository owners automatically when a scheduled workflow run fails (per your notification settings) — no custom alerting code needed, unlike the Apps Script version where I had to wire up `MailApp` manually.

## On the 11pm AWST timing (same note as before, still applies)

ACMA generates the extract around 3am and has it live "by 6am AEDST" — roughly 2-4am AWST. An 11pm AWST run reliably grabs **yesterday's** file. If freshness matters more than the specific hour, change the cron line in the workflow to `'30 20 * * *'` (4:30am AWST) instead — commit the change, no other setup needed.
