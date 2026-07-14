#!/usr/bin/env python3
"""
Prospector — discovery pass.

Finds employers hiring near South Jordan, UT that we don't already track, then
probes each against the tier-1 ATS platforms and writes a ready-to-review list
to `discovered.md`. Meant to run WEEKLY, separate from the daily monitor.

No API keys required — pure HTTP. Does NOT call Anthropic and does NOT touch
companies.json (it only *suggests* additions; you review and add the good ones).

Data source: the public job index behind the Silicon Slopes / Obra job board
(app.obrajobs.com), served by Typesense with a search-only key embedded in that
site's own client. This is an undocumented internal endpoint — it may change or
break without notice. Keep the cadence polite (weekly) and low-volume.
"""
import json, os, re, urllib.request, urllib.parse, datetime

HERE      = os.path.dirname(os.path.abspath(__file__))
COMPANIES = os.path.join(HERE, "companies.json")
OUT       = os.path.join(HERE, "discovered.md")

# --- Obra public search index (search-only key from app.obrajobs.com client) ---
TS_HOST = "2548bdkc7if30qglp.a1.typesense.net"
TS_KEY  = "wy80RmHztDJK2XR6QkC1GjHS7Z79aJWK"
TS_COLL = "jobs"
GEO     = "location:(40.5622, -111.9297, 50 mi)"   # ~50 mi around South Jordan, UT

# Tech discovery via industry buckets; broader/exec discovery via title terms.
INDUSTRIES  = ["information technology", "engineering"]
TITLE_TERMS = ["engineer", "developer", "software", "director", "vice president",
               "operations", "customer success", "customer experience", "professional services"]

# How many top candidates to ATS-probe per run (keeps the run polite/fast).
PROBE_LIMIT = 80

# Obvious staffing / recruiting / aggregator names — not real employers to track.
DENYLIST = {
    "gpac", "insight global", "kforce", "jobot", "consultnet", "mrinetwork", "synergisticit",
    "robert half", "aerotek", "teksystems", "randstad", "adecco", "manpower", "apex systems",
    "cybercoders", "indeed", "ziprecruiter", "lensa", "talentify", "dice", "jobs via dice",
    "get it recruit", "babki", "jabil, inc.", "insight global llc", "varite inc", "varite",
    "trilon group", "consultnet llc",
}

UA = {"User-Agent": "Mozilla/5.0 (prospector discovery)"}


# ---------- Obra / Typesense ----------

def _ts(params):
    url = f"https://{TS_HOST}/collections/{TS_COLL}/documents/search?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": TS_KEY}), timeout=25))

def _facet_companies(filter_by, q="*", query_by=None, max_values=200):
    p = {"q": q, "filter_by": filter_by, "facet_by": "company",
         "max_facet_values": max_values, "per_page": 0}
    if query_by:
        p["query_by"] = query_by
    d = _ts(p)
    return {c["value"]: c["count"] for c in d["facet_counts"][0]["counts"]}

def discover_employers():
    """Return {company_name: {"count": max_role_count, "tech": bool}} for in-area roles.
    'tech' marks companies surfaced by the IT/engineering industry search."""
    found = {}
    def add(name, cnt, tech):
        e = found.setdefault(name, {"count": 0, "tech": False})
        e["count"] = max(e["count"], cnt)
        e["tech"] = e["tech"] or tech
    ind = "[`" + "`,`".join(INDUSTRIES) + "`]"
    try:
        for n, c in _facet_companies(f"{GEO} && industries:={ind}").items():
            add(n, c, True)
    except Exception as e:
        print(f"[warn] industry search failed: {type(e).__name__}")
    for term in TITLE_TERMS:            # one term per query (Typesense ANDs multi-word q)
        try:
            for n, c in _facet_companies(GEO, q=term, query_by="title", max_values=60).items():
                add(n, c, False)
        except Exception as e:
            print(f"[warn] title term '{term}' failed: {type(e).__name__}")
    return found


# ---------- tier-1 ATS probe ----------

def _get_json(url):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8))
    except Exception:
        return None

def _slug_variants(name):
    low = name.lower()
    toks = re.sub(r"[^a-z0-9]+", " ", low).split()
    if not toks:
        return []
    variants = ["".join(toks), "-".join(toks)]
    for suf in ("inc", "llc", "corp", "corporation", "co", "ltd",
                "technologies", "technology", "software", "group", "labs"):
        if toks[-1] == suf and len(toks) > 1:
            variants += ["".join(toks[:-1]), "-".join(toks[:-1])]
    out, seen = [], set()
    for v in variants:
        if v and v not in seen:
            seen.add(v); out.append(v)
    return out[:4]

def probe_ats(name):
    """Return (ats, slug, role_count) for the first tier-1 platform that resolves, else None."""
    for s in _slug_variants(name):
        d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
        if d and d.get("jobs"):
            return ("greenhouse", s, len(d["jobs"]))
        d = _get_json(f"https://api.lever.co/v0/postings/{s}?mode=json")
        if isinstance(d, list) and d:
            return ("lever", s, len(d))
        d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        if d and d.get("jobs"):
            return ("ashby", s, len(d["jobs"]))
        d = _get_json(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1")
        if d and d.get("totalFound", 0) > 0:
            return ("smartrecruiters", s, d["totalFound"])
    return None


# ---------- orchestration ----------

def load_known():
    known, slugs = set(), set()
    for co in json.load(open(COMPANIES)).get("companies", []):
        nm = co["name"].lower().strip()
        known.add(nm)
        known.add(re.sub(r"\s*\(.*?\)", "", nm).strip())   # "beyond (overstock)" -> "beyond"
        if co.get("slug"):
            slugs.add(co["slug"].lower())
    return known, slugs

def config_line(name, ats, slug):
    extra = ' "wd_host": "TODO", "site": "TODO",' if ats == "workday" else ""
    return (f'{{ "name": "{name}", "city": "TODO", "ats": "{ats}",{extra} "slug": "{slug}" }},')

def main():
    known, known_slugs = load_known()
    employers = discover_employers()
    cands = [(n, e["count"], e["tech"]) for n, e in employers.items()
             if n.lower().strip() not in known and n.lower().strip() not in DENYLIST]
    # probe tech-industry candidates first, then the rest, each by role count
    cands.sort(key=lambda x: (not x[2], -x[1]))
    print(f"{len(employers)} employers seen in-area; {len(cands)} new candidates after dedup/denylist.")

    hits, misses, dups = [], [], 0
    for name, cnt, tech in cands[:PROBE_LIMIT]:
        r = probe_ats(name)
        if r and r[1].lower() in known_slugs:   # resolves to a company we already track
            dups += 1
            continue
        (hits if r else misses).append((name, cnt, r))
    print(f"Probed top {min(PROBE_LIMIT, len(cands))}: {len(hits)} new on a tier-1 ATS, "
          f"{dups} were duplicates of tracked companies, {len(misses)} not on tier-1 (custom/Workday/iCIMS).")

    today = datetime.date.today().isoformat()
    L = [f"# Prospector — discovered companies ({today})", "",
         f"Scanned employers hiring within 50 mi of South Jordan. "
         f"{len(cands)} new names after removing ones already tracked and known staffing firms; "
         f"probed the top {min(PROBE_LIMIT, len(cands))} (tech-industry first) against tier-1 ATS platforms.", ""]

    L.append(f"## Ready to add — resolved to a tier-1 ATS ({len(hits)})")
    L.append("_Paste the good ones into `companies.json` (fill in `city`); each was confirmed to return live jobs._\n")
    if hits:
        L.append("```json")
        for name, cnt, (ats, slug, n) in sorted(hits, key=lambda x: -x[1]):
            L.append(f"{config_line(name, ats, slug)}   // ~{cnt} in-area roles; ATS shows {n}")
        L.append("```")
    else:
        L.append("_None this run._")
    L.append("")

    L.append(f"## Seen in-area but no tier-1 ATS ({len(misses)})")
    L.append("_Likely Workday/iCIMS/custom — worth a manual careers-page check for the interesting ones._\n")
    for name, cnt, _ in sorted(misses, key=lambda x: -x[1])[:50]:
        L.append(f"- {name} (~{cnt} in-area roles)")
    L.append("")

    open(OUT, "w").write("\n".join(L))
    print(f"Wrote {OUT} — {len(hits)} ready-to-add, {len(misses)} for manual review.")

if __name__ == "__main__":
    main()
