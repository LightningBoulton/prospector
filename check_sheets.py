#!/usr/bin/env python3
"""
check_sheets.py — verify the Google Sheet webhook without running the daily job.

WHY THIS EXISTS
    The webhook URL lives in a GitHub secret, and the obvious way to test it — run the daily
    workflow — fetches every source, spends Anthropic credit and SENDS EMAIL. That is far too
    much machinery to answer one question: "does the URL work?"

    This asks only that question. It makes exactly one HTTPS POST and touches nothing else.
    No ATS requests, no Anthropic calls, no email, no state written.

USAGE
    In CI:     SHEETS_WEBHOOK_URL is read from the environment (see
               .github/workflows/sheets-check.yml — run it from the Actions tab).
    Locally:   python3 check_sheets.py
               If the variable is not set you are PROMPTED for the URL, and the input is
               hidden — so it never lands in your shell history or on screen.

WHAT IT SENDS
    By default, a single clearly-labelled probe row that writes the header and one obvious
    test line you can delete. With --rows it sends the real records from an existing
    discovery database instead, which is useful once the daily job has run at least once.
"""

import argparse
import getpass
import json
import os
import sys

import lifecycle
import sheets_sync

PROBE_ROW = {
    "job_key": "prospector|connectivity check|delete me",
    "company": "Prospector",
    "title": "Connectivity check — safe to delete this row",
    "canonical_url": "https://github.com/",
    "status": "test",
    "why_fits": "Written by check_sheets.py to confirm the webhook works.",
}


def _resolve_url(explicit=None):
    url = explicit or os.environ.get(sheets_sync.WEBHOOK_ENV)
    if url:
        return url, "environment"
    if not sys.stdin.isatty():
        return None, "unavailable"
    print(f"{sheets_sync.WEBHOOK_ENV} is not set in this shell.")
    print("Paste the Apps Script Web app URL (ends in /exec). Input is hidden.")
    return getpass.getpass("URL: ").strip(), "prompt"


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="check_sheets.py",
        description="Send one request to the Discovery Log webhook and report the result.")
    ap.add_argument("--profile", default="lisa",
                    help="which discovery database to read with --rows (default: lisa)")
    ap.add_argument("--rows", action="store_true",
                    help="send the profile's real records instead of a single probe row")
    ap.add_argument("--url", help="webhook URL (otherwise env var, otherwise prompt)")
    args = ap.parse_args(argv)

    url, source = _resolve_url(args.url)
    if not url:
        print(f"[FAIL] No URL available. Set {sheets_sync.WEBHOOK_ENV} or pass --url.")
        return 2
    # Never print the URL itself — it is the credential.
    print(f"Using webhook URL from {source} (…{url[-12:]})")

    if args.rows:
        here = os.path.dirname(os.path.abspath(__file__))
        db = lifecycle.load_db(args.profile, here)
        rows = sheets_sync.rows_for_db(db)
        if not rows:
            print(f"[note] jobs_{args.profile}.json has no records yet — sending the probe "
                  f"row instead. Run the daily job once to populate it.")
            rows = [sheets_sync.row_for(PROBE_ROW)]
        else:
            print(f"Sending {len(rows)} real record(s) from jobs_{args.profile}.json")
    else:
        rows = [sheets_sync.row_for(PROBE_ROW)]
        print("Sending 1 probe row (delete it from the sheet afterwards).")

    result = sheets_sync.push(rows, url=url, profile=args.profile)
    print(f"\nstatus : {result['status']}")
    print(f"pushed : {result['pushed']} row(s) in {result['batches']} request(s)")
    if result.get("detail"):
        print(f"detail : {result['detail']}")

    if result["status"] == "ok":
        print("\n[OK] The webhook is working. Check the 'Prospector Discovery Log' tab.")
        return 0
    print("\n[FAIL] The webhook did not accept the request. Common causes:")
    print("  * The deployment URL is the /dev one, not /exec — redeploy and copy the")
    print("    Web app URL from the deployment dialog.")
    print("  * 'Who has access' is not set to Anyone, so the POST is unauthenticated and")
    print("    Google returns a login page (HTTP 302/401) instead of running the script.")
    print("  * The script was saved but never deployed, or was edited after deploying")
    print("    without creating a new version.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
