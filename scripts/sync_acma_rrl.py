#!/usr/bin/env python3
"""
ACMA RRL DAILY SYNC (GitHub Actions -> Supabase Postgres)
============================================================
Downloads ACMA's daily "spectra_rrl.zip" Register of Radiocommunications
Licences dataset, unpacks it, and loads each CSV into its own table in
Supabase Postgres under the `acma_rrl` schema. Every table is fully
replaced each run inside a transaction (truncate-and-reload via a staging
table + rename), so a failed run never leaves a table half-written.

WHAT THIS DOES NOT KNOW:
  Table/column names inside spectra_rrl.zip were never confirmed - every
  CSV found is loaded generically, named after its own filename, with
  every column as TEXT (no type guessing). Check acma_rrl._sync_log after
  the first run to see exactly what showed up.

LICENCE TERMS: downloading this dataset means you've agreed to ACMA's
Register of Radiocommunications Licences Usage Conditions (see
https://www.acma.gov.au/radiocomms-licence-data). Client (licensee)
contact details in here must not be used for unsolicited commercial
messages, telemarketing, or mail advertising.

SOURCE RISK: this URL lives on web.acma.gov.au, which ACMA's own site
says was retired from 29 June 2026. Still live as of writing, but could
disappear without notice - a failed run here means a failed GitHub Actions
job, which GitHub emails the repo owner about by default.
============================================================
"""

import csv
import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone

import psycopg2
import requests

ZIP_URL = "https://web.acma.gov.au/rrl-updates/spectra_rrl.zip"
SCHEMA = "acma_rrl"
DB_URL = (os.environ.get("SUPABASE_DB_URL") or "").strip()

# Deliberately excluded from the sync - not a parsing/type issue, just too
# large relative to what it's worth keeping on a size-capped free-tier
# database. applic_text_block is free-text application notes, not used
# anywhere in the search tool, and was ~189MB on its own (2026-07-27).
EXCLUDED_TABLES = {"applic_text_block"}

# Indexes to (re)create on the columns that actually get searched/joined on.
# Necessary because the table object itself is dropped and recreated fresh
# every run (staging-table swap) - any indexes added manually outside this
# script would be silently lost on the next sync. Only the tables large
# enough or joined-on enough to matter are listed; small lookup tables
# (client_type, industry_cat, etc.) don't need one.
TABLE_INDEXES = {
    "licence": ["licence_no", "client_no", "sv_id", "ss_id", "status", "bsl_no"],
    "device_details": ["licence_no", "site_id"],
    "site": ["site_id"],
    "client": ["client_no"],
    "antenna": ["antenna_id"],
    "antenna_pattern": ["antenna_id"],
    "bsl": ["bsl_no", "area_code"],
    "auth_spectrum_freq": ["licence_no"],
    "auth_spectrum_area": ["licence_no"],
}


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def download_zip(dest_path):
    log(f"Downloading {ZIP_URL}")
    with requests.get(ZIP_URL, stream=True, timeout=300,
                       headers={"User-Agent": "acma-rrl-sync/1.0 (+github-actions)"}) as r:
        r.raise_for_status()
        total = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                total += len(chunk)
    log(f"Downloaded {total:,} bytes")
    return total


def sanitize_identifier(raw, prefix_if_needed):
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw.strip()).lower()
    name = re.sub(r"_+", "_", name).strip("_")
    if not name or name[0].isdigit():
        name = f"{prefix_if_needed}_{name}"
    return name[:63]  # Postgres identifier length limit


def sanitize_table_name(filename):
    base = re.sub(r"\.csv$", "", filename, flags=re.IGNORECASE)
    return sanitize_identifier(base, "t")


def sanitize_column_names(header):
    seen = set()
    columns = []
    for i, col in enumerate(header):
        name = sanitize_identifier(col, "c") or f"c_{i}"
        base, n = name, 1
        while name in seen:
            n += 1
            name = f"{base}_{n}"[:63]
        seen.add(name)
        columns.append(name)
    return columns


def read_text_robust(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        log(f"UTF-8 decode failed for {path}, falling back to cp1252")
        return raw.decode("cp1252", errors="replace")


def load_csv_into_table(conn, table_name, csv_path):
    """Loads one CSV into `<schema>.<table_name>`, replacing its contents
    atomically. Returns (row_count, columns). Raises on failure - caller
    decides how to handle a single bad table without aborting the whole run.

    Uses TRUNCATE + COPY back into the same table (when the column set
    hasn't changed) rather than build-a-staging-copy-then-swap. TRUNCATE is
    fully transactional in Postgres, so a failed load still rolls back
    cleanly - but unlike the staging approach, it never needs a second full
    copy of the table to exist on disk at the same time, which is what blew
    the free-tier 1GB disk budget on the 392MB device_details table.
    """
    text = read_text_robust(csv_path)
    header = next(csv.reader(io.StringIO(text)), None)
    if header is None:
        return 0, []
    columns = sanitize_column_names(header)

    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

        cur.execute('''
            select column_name from information_schema.columns
            where table_schema = %s and table_name = %s
            order by ordinal_position
        ''', (SCHEMA, table_name))
        existing_columns = [r[0] for r in cur.fetchall()]

        if existing_columns == columns:
            # Same shape as last run - truncate in place, indexes survive
            # and get maintained automatically as the fresh COPY runs.
            cur.execute(f'TRUNCATE TABLE "{SCHEMA}"."{table_name}"')
        else:
            # First run, or the source column set changed - safe to drop
            # and recreate since there's nothing to preserve either way.
            cur.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{table_name}"')
            cols_ddl = ", ".join(f'"{c}" TEXT' for c in columns)
            cur.execute(f'CREATE TABLE "{SCHEMA}"."{table_name}" ({cols_ddl})')

        cur.copy_expert(
            f'COPY "{SCHEMA}"."{table_name}" FROM STDIN WITH (FORMAT csv, HEADER true)',
            io.StringIO(text)
        )

        cur.execute(f'SELECT COUNT(*) FROM "{SCHEMA}"."{table_name}"')
        row_count = cur.fetchone()[0]

        for col in TABLE_INDEXES.get(table_name, []):
            if col in columns:
                idx_name = f"idx_{table_name}_{col}"[:63]
                cur.execute(
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{SCHEMA}"."{table_name}" ("{col}")'
                )

    conn.commit()
    return row_count, columns


def ensure_log_table(conn):
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS "{SCHEMA}"."_sync_log" (
                id SERIAL PRIMARY KEY,
                run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status TEXT NOT NULL,
                duration_seconds NUMERIC,
                tables JSONB,
                failed_tables JSONB,
                skipped_files JSONB,
                error TEXT
            )
        ''')
    conn.commit()


def write_log(conn, status, duration, tables, failed_tables, skipped, error):
    with conn.cursor() as cur:
        cur.execute(
            f'''INSERT INTO "{SCHEMA}"."_sync_log"
                (status, duration_seconds, tables, failed_tables, skipped_files, error)
                VALUES (%s, %s, %s, %s, %s, %s)''',
            (status, duration, json.dumps(tables), json.dumps(failed_tables),
             json.dumps(skipped), error)
        )
    conn.commit()


def main():
    if not DB_URL:
        log("FAILED: SUPABASE_DB_URL environment variable is not set.")
        sys.exit(1)

    started = time.time()
    conn = psycopg2.connect(DB_URL)
    ensure_log_table(conn)

    table_stats = []
    failed_tables = []
    skipped = []
    top_level_error = None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "spectra_rrl.zip")
            download_zip(zip_path)

            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                log(f"Zip contains {len(names)} file(s): {', '.join(names)}")
                zf.extractall(tmp)

            for name in names:
                if not name.lower().endswith(".csv"):
                    skipped.append(name)
                    log(f"Skipped non-CSV entry: {name}")
                    continue

                table_name = sanitize_table_name(os.path.basename(name))

                if table_name in EXCLUDED_TABLES:
                    skipped.append(name)
                    log(f"Skipped {name}: deliberately excluded (EXCLUDED_TABLES) - size management")
                    continue

                csv_path = os.path.join(tmp, name)
                try:
                    row_count, columns = load_csv_into_table(conn, table_name, csv_path)
                    log(f"Loaded {table_name}: {row_count} rows, {len(columns)} columns")
                    table_stats.append({
                        "file": name, "table": table_name,
                        "rows": row_count, "columns": columns
                    })
                except Exception as e:
                    conn.rollback()
                    log(f"FAILED loading {name} into {table_name}: {e}")
                    failed_tables.append({"file": name, "table": table_name, "error": str(e)})

    except Exception as e:
        top_level_error = str(e)
        log(f"FAILED: {top_level_error}")

    if top_level_error:
        status = "FAILED"
    elif failed_tables:
        status = "PARTIAL"
    else:
        status = "OK"

    duration = round(time.time() - started, 1)
    write_log(conn, status, duration, table_stats, failed_tables, skipped, top_level_error)
    conn.close()

    log(f"Done. status={status} duration={duration}s "
        f"tables_ok={len(table_stats)} tables_failed={len(failed_tables)} skipped={len(skipped)}")

    if status != "OK":
        sys.exit(1)  # non-zero exit -> GitHub Actions marks the run failed -> GitHub emails you


if __name__ == "__main__":
    main()
