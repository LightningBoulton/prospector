#!/usr/bin/env python3
"""Prefetch company logos into logos/<slug>.png for inline CID embedding in the emails.

Run this OCCASIONALLY (not in the daily CI job) to populate / refresh the logos that
jobmonitor.py embeds in each report. The daily run never fetches logos — it just reads
whatever is already in logos/, and falls back to a colored monogram for any company
without a logo file.

    LOGO_DEV_TOKEN=pk_xxx python3 fetch_logos.py            # fetch only missing logos
    LOGO_DEV_TOKEN=pk_xxx python3 fetch_logos.py --force     # re-download everything

Source: logo.dev (https://logo.dev) — grab a free *publishable* token and pass it in the
env; it is used only here and never ends up in the repo or the sent email. Companies and
their domains come from companies.json. A company with no domain, or one whose fetch
fails, is skipped (the engine renders its monogram tile instead).

Commit the resulting logos/*.png so CI has them at send time.
"""
import json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "companies.json")
REMOTE_CONFIG = os.path.join(HERE, "remote_companies.json")
STAFFING_CONFIG = os.path.join(HERE, "staffing_companies.json")
LOGO_DIR = os.path.join(HERE, "logos")


def _load_companies():
    # Union of the local, US-remote, and contract/staffing registries, deduped by slug.
    seen, out = set(), []
    for path in (CONFIG, REMOTE_CONFIG, STAFFING_CONFIG):
        if not os.path.exists(path):
            continue
        for c in json.load(open(path)).get("companies", []):
            if c.get("slug") and c["slug"] not in seen:
                seen.add(c["slug"])
                out.append(c)
    return out


def main():
    token = os.environ.get("LOGO_DEV_TOKEN")
    if not token:
        sys.exit("Set LOGO_DEV_TOKEN (free publishable token from https://logo.dev).")
    force = "--force" in sys.argv[1:]
    os.makedirs(LOGO_DIR, exist_ok=True)
    companies = _load_companies()

    got = skipped = failed = 0
    for c in companies:
        slug, domain = c.get("slug"), c.get("domain")
        if not (slug and domain):
            print(f"  skip   {c['name']}: no domain in companies.json")
            skipped += 1
            continue
        dest = os.path.join(LOGO_DIR, f"{slug}.png")
        if os.path.exists(dest) and not force:
            skipped += 1
            continue
        # size=128 + retina => a crisp source for the 40px tile; format=png keeps alpha.
        url = (f"https://img.logo.dev/{domain}"
               f"?token={token}&size=128&format=png&retina=true")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "prospector-logos"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if not data:
                raise ValueError("empty response")
            with open(dest, "wb") as fh:
                fh.write(data)
            print(f"  ok     {c['name']:<22} -> logos/{slug}.png ({len(data):,} bytes)")
            got += 1
        except Exception as e:
            print(f"  FAIL   {c['name']:<22} ({domain}): {type(e).__name__} {e}")
            failed += 1

    print(f"\nDone. downloaded={got} skipped={skipped} failed={failed} "
          f"(of {len(companies)} companies). Logos live in logos/.")
    if failed:
        print("Companies that failed will render a monogram tile until fetched.")


if __name__ == "__main__":
    main()
