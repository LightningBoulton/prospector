#!/usr/bin/env python3
"""
sheets_sync.py — export Prospector's discovery database to the Google Sheet.

THE SPREADSHEET ARCHITECTURE (as specified)
    Prospector Discovery Log  — every job Prospector has found. Written by this module.
    Application Pipeline      — human-reviewed roles worth pursuing. NEVER written here.
    Applied                   — roles actually submitted. NEVER written here.

Only the Discovery Log is machine-written. Promotion into the Pipeline stays a human act,
which is the whole point of keeping the three tabs separate.

WHY A WEBHOOK AND NOT THE SHEETS API
    Authenticating to the Sheets API needs a service account, and signing its RS256 JWT needs
    a crypto library. This project is standard-library-only by design, and adding `google-auth`
    + `cryptography` to a daily CI job for one CSV append is a poor trade. A Google Apps Script
    Web App bound to the sheet gives the same result over plain HTTPS POST: the script runs as
    the sheet's owner, so no credential ever leaves Google, and Prospector only needs the
    deployment URL. Setup is in PROSPECTOR_V3.md.

    `discovery_log.csv` is written on EVERY run regardless. If the webhook is not configured,
    or fails, the data is still there to import by hand — the export is never the reason a run
    fails, and the sheet is never the system of record. `jobs_<profile>.json` is.
"""

import csv
import datetime
import json
import os
import urllib.error
import urllib.request

WEBHOOK_ENV = "SHEETS_WEBHOOK_URL"
CSV_NAME = "discovery_log.csv"
POST_TIMEOUT = 30
# Apps Script silently truncates very large payloads; batching also means one slow run cannot
# lose everything.
BATCH_SIZE = 200

# Column order for the Discovery Log tab. Append-only: adding a column at the END is safe,
# reordering is not — the Apps Script writes positionally.
COLUMNS = [
    "job_key", "company", "title", "canonical_url", "sources", "posted_date", "first_seen",
    "last_verified", "location", "work_arrangement", "employment_type", "compensation",
    "fit_score", "recommendation", "confidence", "priority", "why_fits", "top_concern",
    "status", "user_decision", "rejection_reason", "closed_date", "removal_reason",
    "tier", "match_reason", "lane", "times_shown", "last_shown",
]


def row_for(rec):
    """One Discovery Log row from one lifecycle record."""
    out = {}
    for col in COLUMNS:
        value = rec.get(col, "")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        elif isinstance(value, bool):
            value = "yes" if value else ""
        elif value is None:
            value = ""
        out[col] = value
    return out


def rows_for_db(db):
    """Every record, newest discovery first — the order a human scanning the tab wants."""
    rows = [row_for(r) for r in db["jobs"].values()]
    rows.sort(key=lambda r: (str(r.get("first_seen") or ""), str(r.get("company") or "")),
              reverse=True)
    return rows


def write_csv(rows, directory, name=CSV_NAME):
    """Always written, webhook or not. This is the fallback import path AND the artifact a
    human can diff in the repo."""
    path = os.path.join(directory, name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def _post(url, payload, timeout=POST_TIMEOUT, opener=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "prospector/1.0"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
        return r.status, r.read(2000).decode("utf-8", "replace")


def push(rows, url=None, profile="", timeout=POST_TIMEOUT, opener=None):
    """Upsert rows into the Prospector Discovery Log tab via the Apps Script Web App.

    Returns a result dict; NEVER raises. A sheet that is unreachable must not fail the daily
    run — the job data is already safe in jobs_<profile>.json and discovery_log.csv."""
    url = url or os.environ.get(WEBHOOK_ENV)
    if not url:
        return {"pushed": 0, "batches": 0, "status": "not_configured",
                "detail": f"{WEBHOOK_ENV} is not set; wrote CSV only"}
    # An empty row set still gets ONE request. There are two reasons: it verifies the
    # endpoint (which is how the connectivity check works before any run has populated the
    # database), and it lets the script write the header row. Without this, "nothing to
    # send" was indistinguishable from "sending failed" — both reported 0 pushed.
    batch_starts = list(range(0, len(rows), BATCH_SIZE)) or [0]
    sent, batches, errors = 0, 0, []
    for i in batch_starts:
        batch = rows[i:i + BATCH_SIZE]
        payload = {"tab": "Prospector Discovery Log", "profile": profile,
                   "generated": datetime.date.today().isoformat(),
                   "key_column": "job_key", "columns": COLUMNS, "rows": batch}
        try:
            status, body = _post(url, payload, timeout, opener)
            if 200 <= status < 300:
                sent += len(batch)
            else:
                errors.append(f"HTTP {status}: {body[:120]}")
        except Exception as e:                    # noqa: BLE001 - export is best-effort
            errors.append(f"{type(e).__name__}: {str(e)[:120]}")
        batches += 1
    if errors:
        status = "partial" if sent else "failed"
    else:
        status = "ok"          # includes the legitimate "nothing new to send" case
    return {"pushed": sent, "batches": batches, "status": status,
            "detail": "; ".join(errors[:3])}


def export(db, directory, profile="", url=None):
    """Write the CSV and (when configured) push to the sheet. Returns a summary for the run
    log."""
    rows = rows_for_db(db)
    csv_path = write_csv(rows, directory, f"discovery_log_{profile}.csv" if profile else CSV_NAME)
    result = push(rows, url=url, profile=profile)
    result["rows"] = len(rows)
    result["csv"] = csv_path
    return result
