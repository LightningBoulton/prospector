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


def _post_json(url, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "prospector/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
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


def _workday_date(text):
    # Workday returns relative strings ("Posted 3 Days Ago"); approximate to a date.
    t, today = (text or "").lower(), datetime.date.today()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - datetime.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return (today - datetime.timedelta(days=30 * int(m.group(1)))).isoformat()
    return ""


def fetch_workday(c):
    # Config: {"ats":"workday","slug":<tenant>,"wd_host":<sub.wdN.myworkdayjobs.com>,"site":<site>}
    host, tenant, site = c["wd_host"], c["slug"], c["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    while True:
        d = _post_json(url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
        for j in d.get("jobPostings", []):
            path = j.get("externalPath", "")
            ext = (j.get("bulletFields") or [path])[0]        # req number is the stable id
            out.append(_norm(c, str(ext), j.get("title", ""), j.get("locationsText", ""),
                             f"https://{host}/{site}{path}", _workday_date(j.get("postedOn", ""))))
        offset += 20
        if offset >= d.get("total", 0) or not d.get("jobPostings"):
            break
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday}


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


def build_report(profile, matched, new, removed, changed, errors, first_run):
    today = datetime.date.today().isoformat()
    L = [f"# {profile['label']}", f"### Job report — {today}", ""]

    # --- What's changed (leads the report) ---
    L.append("## What's changed")
    if first_run:
        L.append("_First run — baseline established. Changes will appear here on the next run._")
    elif not (new or removed or changed):
        L.append("_No changes since the previous run._")
    else:
        if new:
            L.append(f"**New ({len(new)})**")
            L += [f"- **{p['company']}** — [{p['title']}]({p['url']}) · {p['location']}"
                  for p in sorted(new, key=lambda x: x["company"])]
        if changed:
            L.append(f"**Changed titles ({len(changed)})**")
            L += [f"- **{c['company']}** — \"{o['title']}\" → [{c['title']}]({c['url']})"
                  for o, c in changed]
        if removed:
            L.append(f"**Removed / filled ({len(removed)})**")
            L += [f"- **{p['company']}** — {p['title']} · {p['location']}"
                  for p in sorted(removed, key=lambda x: x["company"])]
    L.append("")

    # --- All current matching roles, grouped by company ---
    L.append(f"## All current matching roles ({len(matched)})")
    if not matched:
        L.append("_No roles currently match this profile._")
    else:
        last = None
        for p in sorted(matched, key=lambda x: (x["company"], x["title"])):
            if p["company"] != last:
                L.append(f"\n**{p['company']}**")
                last = p["company"]
            upd = f" · updated {p['updated']}" if p["updated"] else ""
            L.append(f"- [{p['title']}]({p['url']}) · {p['location']}{upd}")
    L.append("")

    # --- Source warnings ---
    if errors:
        L.append(f"## Source warnings ({len(errors)})")
        L += [f"- {e}" for e in errors]
        L.append("")
    return "\n".join(L)


# ---- HTML report (dark-mode, email-safe: inline styles, table layout) ----

# GitHub-dark palette. Kept as explicit 6-digit hex so no client-side blending is needed.
_C = {
    "bg": "#0d1117", "card": "#161b22", "panel": "#1c2128", "border": "#30363d",
    "text": "#c9d1d9", "head": "#f0f6fc", "muted": "#8b949e", "link": "#58a6ff",
    "green": "#3fb950", "amber": "#d29922", "red": "#f85149",
    "green_bg": "#122619", "amber_bg": "#2b2411", "red_bg": "#2d1618",
}
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _esc(s):
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _link(text, url):
    return (f'<a href="{_esc(url)}" style="color:{_C["link"]};text-decoration:none;'
            f'font-weight:600;">{_esc(text)}</a>')


def _section(title):
    return (f'<div style="color:{_C["head"]};font-family:{_FONT};font-size:19px;'
            f'font-weight:700;margin:26px 0 2px;">{_esc(title)}</div>'
            f'<div style="height:1px;background-color:{_C["border"]};margin:10px 0 4px;"></div>')


def _chip(label, key):
    return (f'<div style="margin:16px 0 4px;"><span style="display:inline-block;'
            f'background-color:{_C[key + "_bg"]};color:{_C[key]};font-family:{_FONT};'
            f'font-size:12px;font-weight:700;letter-spacing:.3px;padding:4px 11px;'
            f'border-radius:12px;">{_esc(label)}</span></div>')


def _card(inner, accent):
    return (f'<div style="border:1px solid {_C["border"]};border-left:3px solid {accent};'
            f'background-color:{_C["panel"]};border-radius:8px;padding:11px 14px;'
            f'margin:8px 0;">{inner}</div>')


def _muted(text):
    return (f'<div style="color:{_C["muted"]};font-family:{_FONT};font-size:13px;'
            f'margin-top:4px;">{text}</div>')


def build_html_report(profile, matched, new, removed, changed, errors, first_run):
    today = datetime.date.today().isoformat()
    B = []  # body-cell fragments, mirrors build_report's line list

    # Header
    B.append(f'<div style="color:{_C["head"]};font-family:{_FONT};font-size:22px;'
             f'font-weight:800;line-height:1.3;">{_esc(profile["label"])}</div>')
    B.append(f'<div style="color:{_C["muted"]};font-family:{_FONT};font-size:14px;'
             f'margin-top:4px;">Job report · {today}</div>')

    # What's changed
    B.append(_section("What's changed"))
    if first_run:
        B.append(_muted("First run — baseline established. Changes will appear here on the next run."))
    elif not (new or removed or changed):
        B.append(_muted("No changes since the previous run."))
    else:
        if new:
            B.append(_chip(f"New · {len(new)}", "green"))
            for p in sorted(new, key=lambda x: x["company"]):
                inner = (_link(p["title"], p["url"])
                         + _muted(f'{_esc(p["company"])} · {_esc(p["location"])}'))
                B.append(_card(inner, _C["green"]))
        if changed:
            B.append(_chip(f"Changed titles · {len(changed)}", "amber"))
            for o, c in changed:
                inner = (_link(c["title"], c["url"])
                         + _muted(f'{_esc(c["company"])} · was "{_esc(o["title"])}"'))
                B.append(_card(inner, _C["amber"]))
        if removed:
            B.append(_chip(f"Removed / filled · {len(removed)}", "red"))
            for p in sorted(removed, key=lambda x: x["company"]):
                inner = (f'<span style="color:{_C["text"]};font-family:{_FONT};'
                         f'font-weight:600;">{_esc(p["title"])}</span>'
                         + _muted(f'{_esc(p["company"])} · {_esc(p["location"])}'))
                B.append(_card(inner, _C["red"]))

    # All current matching roles
    B.append(_section(f"All current matching roles ({len(matched)})"))
    if not matched:
        B.append(_muted("No roles currently match this profile."))
    else:
        last = None
        for p in sorted(matched, key=lambda x: (x["company"], x["title"])):
            if p["company"] != last:
                B.append(f'<div style="color:{_C["head"]};font-family:{_FONT};'
                         f'font-weight:700;font-size:15px;margin:18px 0 6px;">'
                         f'{_esc(p["company"])}</div>')
                last = p["company"]
            upd = f' · updated {_esc(p["updated"])}' if p["updated"] else ""
            B.append(f'<div style="margin:0 0 6px;line-height:1.45;">'
                     f'{_link(p["title"], p["url"])}'
                     f'<span style="color:{_C["muted"]};font-family:{_FONT};font-size:13px;">'
                     f' · {_esc(p["location"])}{upd}</span></div>')

    # Source warnings
    if errors:
        B.append(_section(f"Source warnings ({len(errors)})"))
        for e in errors:
            B.append(f'<div style="color:{_C["amber"]};font-family:{_FONT};'
                     f'font-size:13px;margin:0 0 4px;">{_esc(e)}</div>')

    body = "".join(B)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_esc(profile["label"])}</title>
</head>
<body style="margin:0;padding:0;background-color:{_C['bg']};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_C['bg']};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background-color:{_C['card']};border:1px solid {_C['border']};border-radius:14px;">
<tr><td style="padding:28px 30px 32px;">{body}</td></tr>
</table>
<div style="color:{_C['muted']};font-family:{_FONT};font-size:11px;margin-top:16px;">Prospector · Silicon Slopes job monitor</div>
</td></tr>
</table>
</body>
</html>"""


def run_profile(profile, pool, errors):
    matched = [p for p in pool if matches_profile(p, profile)]
    snap = os.path.join(HERE, f"snapshot_{profile['name']}.json")
    rpt  = os.path.join(HERE, f"report_{profile['name']}.md")
    html = os.path.join(HERE, f"report_{profile['name']}.html")
    prev = json.load(open(snap)) if os.path.exists(snap) else None
    if prev is None:
        new, removed, changed, first = [], [], [], True
    else:
        new, removed, changed = diff(prev, matched)
        first = False
    report = build_report(profile, matched, new, removed, changed, errors, first_run=first)
    report_html = build_html_report(profile, matched, new, removed, changed, errors, first_run=first)
    json.dump(matched, open(snap, "w"), indent=1)
    open(rpt, "w").write(report)
    open(html, "w").write(report_html)
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
