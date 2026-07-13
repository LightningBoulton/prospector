#!/usr/bin/env python3
"""
jobmonitor.py — daily job-posting monitor for Silicon Slopes tech companies.

Fetches every company once, then runs one or more *profiles* against the results.
A profile is a named title filter (see profiles.json). Each profile keeps its own
snapshot and produces its own report, so diffs never collide between people.

Usage:
  python3 jobmonitor.py                 # run every enabled profile
  python3 jobmonitor.py --profile lisa  # run just one profile
  python3 jobmonitor.py --list          # list available profiles

No Playwright, no HTML scraping — every source here is a structured JSON API.
"""

import json, os, re, sys, urllib.request, datetime

HERE     = os.path.dirname(os.path.abspath(__file__))
CONFIG   = os.path.join(HERE, "companies.json")
PROFILES = os.path.join(HERE, "profiles.json")

# Global location gate, applied once to the fetched pool before any profile runs.
LOCAL_KEYWORDS = [
    "ut", "utah", "salt lake", "south jordan", "lehi", "draper", "sandy",
    "provo", "orem", "american fork", "lindon", "pleasant grove", "cottonwood",
    "midvale", "murray", "west jordan", "riverton", "bluffdale", "herriman",
]
KEEP_REMOTE = True
LOCAL_ONLY  = True


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "prospector/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


# ---- per-ATS fetchers, each returning normalized postings ----

def fetch_greenhouse(c):
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    return [_norm(c, str(j["id"]), j.get("title", ""),
                  (j.get("location") or {}).get("name", ""),
                  j.get("absolute_url", ""), (j.get("updated_at") or "")[:10])
            for j in d.get("jobs", [])]


def fetch_lever(c):
    out = []
    for j in _get(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json"):
        created = j.get("createdAt")
        date = (datetime.datetime.fromtimestamp(created / 1000, datetime.timezone.utc)
                .date().isoformat() if created else "")
        out.append(_norm(c, str(j["id"]), j.get("text", ""),
                         (j.get("categories") or {}).get("location", ""),
                         j.get("hostedUrl", ""), date))
    return out


def fetch_smartrecruiters(c):
    out, offset = [], 0
    while True:
        d = _get(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100&offset={offset}")
        for j in d.get("content", []):
            loc = j.get("location") or {}
            loc_str = loc.get("fullLocation") or ", ".join(
                x for x in [loc.get("city"), loc.get("region")] if x)
            if loc.get("remote"):
                loc_str = (loc_str + " (Remote)").strip()
            out.append(_norm(c, str(j["id"]), j.get("name", ""), loc_str,
                             f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                             (j.get("releasedDate") or "")[:10]))
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "smartrecruiters": fetch_smartrecruiters}


def _norm(company, ext_id, title, location, url, updated):
    return {"key": f"{company['name']}::{ext_id}", "company": company["name"],
            "title": title.strip(), "location": location.strip(),
            "url": url, "updated": updated}


def is_local(loc):
    l = loc.lower()
    if KEEP_REMOTE and "remote" in l:
        return True
    return any(k in l for k in LOCAL_KEYWORDS)


# ---- profile matching (word-boundary aware) ----

def _any_term(title, terms):
    return any(re.search(r"\b" + re.escape(t) + r"\b", title) for t in terms)


def matches_profile(posting, profile):
    title = posting["title"].lower()
    if _any_term(title, profile.get("exclude_any", [])):
        return False
    for group in profile.get("match_groups", []):
        if not _any_term(title, group):
            return False
    return True


# ---- pipeline ----

def collect_pool():
    cfg = json.load(open(CONFIG))
    pool, errors = [], []
    for c in cfg["companies"]:
        try:
            got = FETCHERS[c["ats"]](c)
            if LOCAL_ONLY:
                got = [p for p in got if is_local(p["location"])]
            pool.extend(got)
        except Exception as e:
            errors.append(f"{c['name']} ({c['ats']}/{c['slug']}): {type(e).__name__} {e}")
    return pool, errors


def diff(prev, curr):
    pmap, cmap = {p["key"]: p for p in prev}, {p["key"]: p for p in curr}
    new     = [cmap[k] for k in cmap if k not in pmap]
    removed = [pmap[k] for k in pmap if k not in cmap]
    changed = [(pmap[k], cmap[k]) for k in cmap
               if k in pmap and pmap[k]["title"] != cmap[k]["title"]]
    return new, removed, changed


def build_report(profile, new, removed, changed, total, errors, first_run):
    today = datetime.date.today().isoformat()
    L = [f"# {profile['label']}", f"### Job report — {today}", ""]
    if first_run:
        L.append(f"First run — baseline set. Tracking **{total}** matching roles. "
                 f"Tomorrow's run will show what changed.")
        return "\n".join(L) + "\n"
    L.append(f"Tracking **{total}** matching roles.")
    L.append("")
    if not (new or removed or changed):
        L.append("_No changes since the previous run._")
    if new:
        L.append(f"## New postings ({len(new)})")
        L += [f"- **{p['company']}** — [{p['title']}]({p['url']}) · {p['location']}"
              for p in sorted(new, key=lambda x: x["company"])]
        L.append("")
    if changed:
        L.append(f"## Changed titles ({len(changed)})")
        L += [f"- **{c['company']}** — \"{o['title']}\" → [{c['title']}]({c['url']})"
              for o, c in changed]
        L.append("")
    if removed:
        L.append(f"## Removed / filled ({len(removed)})")
        L += [f"- **{p['company']}** — {p['title']} · {p['location']}"
              for p in sorted(removed, key=lambda x: x["company"])]
        L.append("")
    if errors:
        L.append(f"## Source warnings ({len(errors)})")
        L += [f"- {e}" for e in errors]
        L.append("")
    return "\n".join(L)


def run_profile(profile, pool, errors):
    matched = [p for p in pool if matches_profile(p, profile)]
    snap = os.path.join(HERE, f"snapshot_{profile['name']}.json")
    rpt  = os.path.join(HERE, f"report_{profile['name']}.md")
    prev = json.load(open(snap)) if os.path.exists(snap) else None
    if prev is None:
        report = build_report(profile, [], [], [], len(matched), errors, first_run=True)
    else:
        new, removed, changed = diff(prev, matched)
        report = build_report(profile, new, removed, changed, len(matched), errors, first_run=False)
    json.dump(matched, open(snap, "w"), indent=1)
    open(rpt, "w").write(report)
    return matched, report


def main():
    args = sys.argv[1:]
    profiles = json.load(open(PROFILES))["profiles"]

    if "--list" in args:
        for p in profiles:
            state = "on " if p.get("enabled", True) else "off"
            print(f"  [{state}] {p['name']:<8} {p['label']}")
        return

    if "--profile" in args:
        name = args[args.index("--profile") + 1]
        profiles = [p for p in profiles if p["name"] == name]
        if not profiles:
            sys.exit(f"No profile named '{name}'. Try --list.")
    else:
        profiles = [p for p in profiles if p.get("enabled", True)]

    pool, errors = collect_pool()
    print(f"Fetched {len(pool)} local/remote roles across all companies.\n")
    for p in profiles:
        matched, report = run_profile(p, pool, errors)
        print("=" * 70)
        print(report)


if __name__ == "__main__":
    main()
