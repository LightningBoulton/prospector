#!/usr/bin/env python3
"""Quarantine gate for the logo-refresh workflow: a broken download never gets committed.

Run by .github/workflows/logos.yml AFTER fetch_logos.py and BEFORE the commit step.
Also runnable locally:  python3 .github/scripts/verify_logos.py

WHY THIS EXISTS
That workflow commits BINARY files fetched from a third party with nobody inspecting the
result — a git diff on a PNG tells a reviewer nothing. fetch_logos.py only checks that the
response body is non-empty, so an HTML error page or a truncated body gets written to disk and
the script still exits 0. Committed, that renders as a broken image in the email.

WHY IT QUARANTINES RATHER THAN FAILING THE JOB
Deleting the bad file and continuing beats failing the run. The likely failure is one flaky
domain, not an outage — and hard-failing would throw away the other sixteen good logos and
leave you re-running into the same wall. Removing the file restores exactly the behavior the
engine already has for a company with no logo: `_logo_square` renders a colored monogram
instead. The company is simply retried on the next run, having cost nothing.

Exit code is always 0 by design. Removals are reported as GitHub Actions warnings and in the
step summary, so a persistent problem is still visible in the run without blocking it.
"""
import collections
import glob
import hashlib
import os
import sys

# A real 128px retina logo runs to a few KB; the smallest legitimately observed was ~3.7KB.
# Anything under this is far likelier to be an error body or a truncated response than a mark.
MIN_BYTES = 512
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IEND = b"IEND"


def _problem(data):
    """Why this file is not a usable logo, or None if it is fine."""
    if not data.startswith(PNG_MAGIC):
        head = data[:60].decode("utf-8", "replace").replace("\n", " ").strip()
        return f"not a PNG (body starts with {head!r})"
    if IEND not in data[-1024:]:
        return "PNG is truncated (no IEND chunk near the end)"
    if len(data) < MIN_BYTES:
        return f"only {len(data)} bytes — implausibly small for a logo"
    return None


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    logo_dir = os.path.join(root, "logos")
    files = sorted(glob.glob(os.path.join(logo_dir, "*.png")))
    if not files:
        print("::warning::no logo files found in logos/ — did fetch_logos.py run?")
        return 0

    removed, by_hash = [], collections.defaultdict(list)
    for path in files:
        name = os.path.basename(path)
        data = open(path, "rb").read()
        why = _problem(data)
        if why:
            os.remove(path)
            removed.append((name, why))
            print(f"::warning::removed {name}: {why} — that company keeps its monogram "
                  f"tile and will be retried on the next run")
            continue
        by_hash[hashlib.md5(data).hexdigest()].append(name)

    # Identical bytes only warn: a parent and subsidiary can legitimately share a mark, so
    # removing on that basis would discard a real logo. But it is also how logo.dev's generic
    # fallback looks when it has no logo for a domain, which is worth surfacing.
    dupes = {h: n for h, n in by_hash.items() if len(n) > 1}
    for group in dupes.values():
        print("::warning::identical logo files — one may be logo.dev's generic fallback "
              f"rather than a real mark: {', '.join(sorted(group))}")

    kept = len(files) - len(removed)
    print(f"\nChecked {len(files)} file(s): {kept} valid, {len(removed)} removed, "
          f"{len(dupes)} duplicate group(s).")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"### Logo verification\n\n{kept} valid, {len(removed)} removed.\n\n")
            for name, why in removed:
                fh.write(f"- **removed** `{name}` — {why}\n")
            for group in dupes.values():
                fh.write(f"- duplicate bytes: {', '.join(sorted(group))}\n")

    if removed:
        print("Bad downloads were removed, so only valid logos will be committed.")
    else:
        print("All logos are valid PNGs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
