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

import argparse, glob, hashlib, html, json, os, re, shutil, sys, urllib.request, datetime

HERE     = os.path.dirname(os.path.abspath(__file__))
CONFIG   = os.path.join(HERE, "companies.json")
REMOTE_CONFIG = os.path.join(HERE, "remote_companies.json")  # US-remote lane registry
STAFFING_CONFIG = os.path.join(HERE, "staffing_companies.json")  # contract/staffing lane registry
PROFILES = os.path.join(HERE, "profiles.json")
SETTINGS = os.path.join(HERE, "settings.json")

# Where per-profile STATE (snapshots) and OUTPUT (reports) are written. Both default to the
# repo root, which is what the daily CI run wants — production snapshots live there and are
# committed back. `--snapshot-dir` / `--out-dir` / `--dry-run` redirect them so a local or
# test run can NEVER clobber committed production state. Set once in main().
SNAPSHOT_DIR = HERE
OUT_DIR      = HERE

# Run-wide tweakables (settings.json). Defaults apply if the file or a key is missing.
SETTINGS_DEFAULTS = {"max_posting_age_days": 90, "fit_scoring_enabled": True,
                     "star_within_days": 7, "allow_international_remote": False}

# How recent a posting must be to earn a ⭐ in the report. Set from settings in main().
STAR_WITHIN_DAYS = SETTINGS_DEFAULTS["star_within_days"]

# When False, remote roles that name a non-US country are dropped. Set from settings in main().
ALLOW_INTL_REMOTE = SETTINGS_DEFAULTS["allow_international_remote"]


def load_settings():
    s = dict(SETTINGS_DEFAULTS)
    try:
        s.update({k: v for k, v in json.load(open(SETTINGS)).items() if not k.startswith("_")})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[warn] settings.json unreadable ({type(e).__name__}); using defaults.")
    return s

# LLM fit-scoring (optional). Active only when ANTHROPIC_API_KEY is set AND a profile
# names a background_file. Falls back to no scoring otherwise — the tool still runs fine.
FIT_MODEL = "claude-sonnet-5"   # judgment task; swap to "claude-haiku-4-5" for lower cost
DESC_LIMIT = 2000               # chars of job description sent to the model

# Version of the verdict SHAPE that score_fit returns. It is folded into the cache
# fingerprint (`_bg_fingerprint`), so bumping it invalidates every stored verdict and forces
# one full re-score. Bump it whenever the fields or their meaning change — that is what stops
# new rendering code from reading a stale verdict written under the old schema.
FIT_SCHEMA_VERSION = 2

# Global location gate, applied once to the fetched pool before any profile runs.
LOCAL_KEYWORDS = [
    "ut", "utah", "salt lake", "south jordan", "lehi", "draper", "sandy",
    "provo", "orem", "american fork", "lindon", "pleasant grove", "cottonwood",
    "midvale", "murray", "west jordan", "riverton", "bluffdale", "herriman",
]
KEEP_REMOTE = True
LOCAL_ONLY  = True

# Non-US country/region tokens for the international-remote filter (word-boundary matched).
# Ambiguous names that collide with US places are intentionally OMITTED — "Georgia",
# "Mexico" (→ New Mexico), "Jordan" (→ South Jordan). Extend as new ones surface.
INTERNATIONAL_MARKERS = [
    "united kingdom", "uk", "england", "scotland", "wales", "ireland", "britain",
    "canada", "brazil", "argentina", "chile", "colombia", "peru",
    "india", "pakistan", "bangladesh", "sri lanka", "philippines", "china", "hong kong",
    "japan", "korea", "singapore", "malaysia", "indonesia", "vietnam", "thailand", "taiwan",
    "australia", "new zealand",
    "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium", "poland",
    "romania", "bulgaria", "ukraine", "czech", "hungary", "sweden", "norway", "denmark",
    "finland", "switzerland", "austria", "greece", "turkey", "serbia", "croatia",
    "lithuania", "estonia", "latvia", "slovakia", "slovenia",
    "israel", "egypt", "south africa", "nigeria", "kenya", "morocco",
    "uae", "united arab emirates", "dubai", "abu dhabi", "saudi arabia", "qatar",
    "emea", "apac", "latam", "europe", "asia pacific",
]


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "prospector/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _get_text(url):
    # Raw-bytes fetch for non-JSON sources (e.g. Personio's XML feed).
    req = urllib.request.Request(url, headers={"User-Agent": "prospector/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def _post_json(url, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "prospector/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _clean_html(s, limit=DESC_LIMIT):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:limit]


# ---- per-ATS fetchers, each returning normalized postings ----

def fetch_greenhouse(c):
    # `first_published` is the true post date (`updated_at` is edit time). `content=true`
    # yields the description used for both LLM scoring and salary regex.
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        jid = str(j["id"])
        out.append(_norm(c, jid, j.get("title", ""),
                         (j.get("location") or {}).get("name", ""),
                         j.get("absolute_url", ""),
                         (j.get("first_published") or j.get("updated_at") or "")[:10],
                         ats="greenhouse",
                         detail_url=f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs/{jid}",
                         description=_clean_html(j.get("content", ""))))
    return out


def _lever_salary(j):
    # Lever exposes pay in the list response: prefer the structured range, else the prose.
    r = j.get("salaryRange") or {}
    if r.get("min") and r.get("max"):
        return _fmt_pay(r["min"], r["max"], r.get("currency", "USD"), r.get("interval"))
    return (j.get("salaryDescriptionPlain") or "").strip() or None


def fetch_lever(c):
    out = []
    for j in _get(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json"):
        created = j.get("createdAt")
        date = (datetime.datetime.fromtimestamp(created / 1000, datetime.timezone.utc)
                .date().isoformat() if created else "")
        out.append(_norm(c, str(j["id"]), j.get("text", ""),
                         (j.get("categories") or {}).get("location", ""),
                         j.get("hostedUrl", ""), date,
                         salary=_lever_salary(j), ats="lever",
                         description=(j.get("descriptionPlain") or "")[:DESC_LIMIT]))
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
                             (j.get("releasedDate") or "")[:10], ats="smartrecruiters",
                             detail_url=f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{j['id']}"))
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
    # Optional "search_text" scopes a large tenant server-side (e.g. "Lehi") so we don't
    # page through thousands of global roles. Multi-location hits return "N Locations";
    # we relabel those with the search term so the local gate keeps them and they read well.
    host, tenant, site = c["wd_host"], c["slug"], c["site"]
    search_text = c.get("search_text", "")
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset, total = [], 0, None
    while True:
        d = _post_json(url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search_text})
        if total is None:
            total = d.get("total", 0)      # only the FIRST page reports the real total
        page = d.get("jobPostings", [])
        for j in page:
            path = j.get("externalPath", "")
            ext = (j.get("bulletFields") or [path])[0]        # req number is the stable id
            loc = j.get("locationsText", "")
            m = re.match(r"\s*(\d+)\s+locations", loc, re.I)
            if search_text and m:
                loc = f"{search_text} (+{int(m.group(1)) - 1} more)"
            out.append(_norm(c, str(ext), j.get("title", ""), loc,
                             f"https://{host}/{site}{path}", _workday_date(j.get("postedOn", "")),
                             ats="workday",
                             detail_url=f"https://{host}/wday/cxs/{tenant}/{site}{path}"))
        offset += 20
        if offset >= total or not page:
            break
    return out


def fetch_ashby(c):
    # Ashby public job board: GET posting-api/job-board/{slug} -> {jobs:[{id,title,location,
    # isRemote,jobUrl,publishedAt,descriptionPlain,compensation}]}. `publishedAt` -> posted;
    # `descriptionPlain` feeds LLM/salary; salary sometimes in the compensation summary.
    # 404s on a bad slug (caught upstream).
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or "").strip()
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip() if loc else "Remote"
        comp = j.get("compensation") or {}
        salary = (comp.get("compensationTierSummary")
                  or comp.get("scrapeableCompensationSalarySummary") or None)
        if salary:  # Ashby summaries can trail equity/benefits prose; keep just the pay range
            salary = salary.split("•")[0].strip() or None
        out.append(_norm(c, str(j["id"]), j.get("title", ""), loc,
                         j.get("jobUrl", ""), (j.get("publishedAt") or "")[:10],
                         salary=salary, ats="ashby",
                         description=(j.get("descriptionPlain") or "")[:DESC_LIMIT]))
    return out


def fetch_recruitee(c):
    # Recruitee careers API: GET {slug}.recruitee.com/api/offers/ -> {offers:[{id,title,
    # location,city,country,careers_url,published_at,created_at,description,remote,salary}]}.
    # No auth. 404s on a bad slug (caught upstream).
    d = _get(f"https://{c['slug']}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        loc = j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        if j.get("remote") and "remote" not in (loc or "").lower():
            loc = (loc + " (Remote)").strip() if loc else "Remote"
        sal = j.get("salary")
        salary = sal.strip() if isinstance(sal, str) and sal.strip() else None
        posted = (j.get("published_at") or j.get("created_at") or "")[:10]
        out.append(_norm(c, str(j["id"]), j.get("title", ""), loc or "",
                         j.get("careers_url") or j.get("careers_apply_url") or "", posted,
                         salary=salary, ats="recruitee",
                         description=_clean_html(j.get("description", ""))))
    return out


def fetch_personio(c):
    # Personio public XML feed: GET {slug}.jobs.personio.com/xml?language=en -> <position>
    # elements (id,name,office,additionalOffices,department,createdAt,jobDescriptions). No JSON
    # and no apply URL in the feed, so build the canonical job URL from slug+id. 404s on bad slug.
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_get_text(f"https://{c['slug']}.jobs.personio.com/xml?language=en"))
    out = []
    for pos in root.findall(".//position"):
        def _t(tag):
            el = pos.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        pid = _t("id")
        if not pid:
            continue
        offices = [_t("office")] + [o.text for o in pos.findall("additionalOffices/office") if o.text]
        loc = ", ".join(x for x in offices if x)
        desc = " ".join(d.text or "" for d in pos.findall("jobDescriptions/jobDescription/value"))
        out.append(_norm(c, pid, _t("name"), loc,
                         f"https://{c['slug']}.jobs.personio.com/job/{pid}?language=en",
                         _t("createdAt")[:10], ats="personio", description=_clean_html(desc)))
    return out


def _rss_date(text):
    # RFC-822 pubDate ("Thu, 23 Jul 2026 18:53:50 GMT") -> "YYYY-MM-DD" ("" if unparseable).
    if not text:
        return ""
    try:
        import email.utils
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except Exception:
        return ""


# Aquent's placement_type values that signal CONTRACT work. The staffing lane exists ONLY
# for contract roles, so fetch_aquent surfaces contract-ish placements (and any unknown
# value) and drops clearly-permanent ones at the source — placement_type is structured, so
# we filter on data instead of guessing "contract" from the title. "Temp to Perm" keeps
# (it contains "temp"); "Permanent"/"Direct Hire" drop.
_AQUENT_FEED = "https://aquent.com/feeds/jobs.xml"
_CONTRACT_PLACEMENT = ("temporary", "temp", "contract", "freelance", "interim", "c2h")


def fetch_aquent(c):
    # Aquent national jobs feed: GET aquent.com/feeds/jobs.xml -> RSS <item> elements with
    # {job_id, title ("Role [job_id]"), location{city,state,country}, placement_type,
    # remotetype ("Fully remote"), salary ("$45-48 Hourly"), description (HTML), pubDate
    # (RFC-822), url}. No auth and no per-company slug — ONE national feed for all of Aquent,
    # so the fetcher ignores slug for the URL (slug is only the registry/logo identity; an
    # optional "feed_url" in config overrides). Only contract-type placements are kept (this
    # is the contract lane). remotetype is folded into the location string so the location
    # gate keeps remote roles (the location itself is often just "US"). salary and description
    # are inline — no per-posting detail fetch needed. Config: {ats:"aquent","slug":"aquent"}.
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_get_text(c.get("feed_url", _AQUENT_FEED)))
    out = []
    for it in root.findall(".//item"):
        def _t(tag):
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        jid = _t("job_id")
        if not jid:
            continue
        pt = _t("placement_type").lower()
        if pt and not any(m in pt for m in _CONTRACT_PLACEMENT):
            continue   # clearly-permanent placement — out of scope for the contract lane
        loc_el = it.find("location")
        country = (loc_el.findtext("country") or "").strip() if loc_el is not None else ""
        # Aquent gives a clean ISO country code, so drop non-US roles precisely HERE — the
        # string gate's INTERNATIONAL_MARKERS are full names and miss codes like "FR"/"GB"
        # (adding 2-letter codes there is unsafe: "it"/"in"/"no" collide with English words).
        # Honor allow_international_remote exactly like the global gate does.
        if country and country.upper() not in ("US", "USA") and not ALLOW_INTL_REMOTE:
            continue
        loc = html.unescape(", ".join(x for x in [
            (loc_el.findtext("city") or "").strip() if loc_el is not None else "",
            (loc_el.findtext("state") or "").strip() if loc_el is not None else "",
            country] if x))
        if "remote" in _t("remotetype").lower() and "remote" not in loc.lower():
            loc = (loc + " (Remote)").strip() if loc else "Remote"
        # CDATA titles aren't entity-decoded by the XML parser; unescape "&amp;" etc.
        title = re.sub(r"\s*\[\d+\]\s*$", "", html.unescape(_t("title")))
        # The salary field sometimes carries employment-type notes ("W2", "part-time…")
        # rather than pay; keep it only when it names an actual currency amount.
        raw_sal = _t("salary")
        salary = raw_sal if raw_sal and any(sym in raw_sal for sym in "$€£") else None
        out.append(_norm(c, jid, title, loc, _t("url"), _rss_date(_t("pubDate")),
                         salary=salary, ats="aquent",
                         description=_clean_html(_t("description"))))
    return out


_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
# SnapHop careersite feeds carry no structured placement type; these are contract-heavy
# staffing shops, so (like Aquent) keep everything EXCEPT roles explicitly flagged permanent.
_PERMANENT_MARKERS = ("permanent", "direct hire", "direct-hire", "perm placement")
_US_STATE_ABBR = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
                  "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
                  "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
                  "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"}
_US_STATE_NAMES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado",
                   "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
                   "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
                   "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
                   "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
                   "new mexico", "new york", "north carolina", "north dakota", "ohio",
                   "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
                   "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
                   "washington", "west virginia", "wisconsin", "wyoming"}


def _snaphop_location(title, summary_html, href):
    # Best-effort location for a SnapHop careersite entry (feeds carry no structured location).
    # Remote signal: the URL slug ends "-anywhere", OR the TITLE says remote (Addison's
    # "USA Remote - …" convention), OR the summary's first line is EXACTLY a remote phrase —
    # deliberately NOT any "remote" deep in the summary, which would misread Eliassen hybrids
    # ("2 days remote in Boston"). Otherwise derive "City, ST" from the summary's location
    # line (Eliassen) or the slug tail — which ends in a 2-letter code (Eliassen: "orange-ca")
    # OR a full state name (Addison: "…-virginia", "…-metro-area-texas").
    slug = re.sub(r"-[a-z0-9]{15,18}$", "", (href or "").rstrip("/").split("/")[-1])
    first_p = ""
    m = re.search(r"<p>(.*?)</p>", summary_html or "", re.S)
    if m:
        first_p = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    if (slug.endswith("-anywhere") or "remote" in (title or "").lower()
            or first_p.lower() in ("remote", "fully remote", "100% remote", "usa remote", "us remote")):
        return "Remote"
    cm = re.search(r"\bin\s+([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b", first_p)  # "… in City, ST"
    if cm:
        return cm.group(1).strip()
    toks = slug.split("-")
    if toks and toks[-1].lower() in _US_STATE_ABBR:
        city = toks[-2].title() if len(toks) >= 2 else ""
        return f"{city}, {toks[-1].upper()}" if city else toks[-1].upper()
    for span in (2, 1):                       # full state name may be 1-2 slug tokens
        if len(toks) >= span and " ".join(toks[-span:]).lower() in _US_STATE_NAMES:
            return " ".join(toks[-span:]).title()
    return ""


def fetch_snaphop(c):
    # SnapHop careersite Atom feed (used by staffing firms on Bullhorn-for-Salesforce, e.g.
    # Eliassen, Addison Group): GET careers.{domain}/feeds/jobs.atom -> Atom <entry>s {id
    # ("tag:…:/<uuid>" = stable key), title, summary (HTML), published/updated (ISO), link
    # href (slug ends "<city>-<state>" or "-anywhere", then a Salesforce id)}. Returns the
    # ~100 most-recent roles (NO pagination). No structured location/type/salary: location via
    # `_snaphop_location`, contract filtering keep-unless-permanent, salary left to
    # enrich_salary. Feed URL derived from `domain` (override with `feed_url`). Config
    # {ats:"snaphop","slug":<logo id>,"domain":<firm domain>}.
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_get_text(c.get("feed_url") or f"https://careers.{c['domain']}/feeds/jobs.atom"))
    out = []
    for e in root.findall("a:entry", _ATOM_NS):
        def _t(tag):
            el = e.find(f"a:{tag}", _ATOM_NS)
            return (el.text or "").strip() if el is not None and el.text else ""
        jid = _t("id").rstrip("/").split("/")[-1]   # UUID from the tag: URI
        if not jid:
            continue
        sel = e.find("a:summary", _ATOM_NS)
        summary = (sel.text or "") if sel is not None else ""
        title = html.unescape(_t("title"))
        body = (summary + " " + title).lower()
        if any(m in body for m in _PERMANENT_MARKERS) and "contract" not in body:
            continue   # explicitly permanent placement — out of scope for the contract lane
        link_el = e.find("a:link", _ATOM_NS)
        href = ((link_el.attrib.get("href") if link_el is not None else "") or "").replace("http://", "https://")
        out.append(_norm(c, jid, title, _snaphop_location(title, summary, href),
                         href, (_t("published") or _t("updated"))[:10], ats="snaphop",
                         description=_clean_html(summary)))
    return out


def _phenom_payload(frm, size):
    # Minimal refineSearch query the Phenom /widgets endpoint accepts; `from`/`size` page it.
    return {"lang": "en_us", "deviceType": "desktop", "country": "us",
            "pageName": "search-results", "ddoKey": "refineSearch", "from": frm, "size": size,
            "jobs": True, "counts": True, "keywords": "", "global": True, "siteType": "external",
            "clearAll": False, "jdsource": "facets", "pageId": "page1", "selected_facets": {}}


def fetch_phenom(c):
    # Phenom (phenompeople.com) careersite search API — the front end many large employers put
    # over their real ATS (Circle's applyUrls are Workday). POST https://{host}/widgets (host
    # defaults to careers.{domain}) with a paged refineSearch query -> {refineSearch:{totalHits,
    # data:{jobs:[{jobId,title,cityStateCountry,isMultiLocation,multi_location,applyUrl,
    # postedDate,descriptionTeaser}]}}}. No auth. Stable id = jobId (req number). GOTCHA: there
    # is NO "remote" in the location field even for remote roles — remote lives only in the
    # Workday applyUrl path ("…-remote-first-in-US"), so fold it into the location string so the
    # US-remote gate keeps it. Config: {ats:"phenom","slug":<logo id>,"domain":<firm domain>
    # [,"phenom_host":<careers host if not careers.{domain}>]}.
    host = c.get("phenom_host") or f"careers.{c['domain']}"
    url = f"https://{host}/widgets"
    out, frm, size, total = [], 0, 100, None
    while True:
        rs = (_post_json(url, _phenom_payload(frm, size)) or {}).get("refineSearch") or {}
        if total is None:
            total = rs.get("totalHits", 0)
        jobs = (rs.get("data") or {}).get("jobs") or []
        for j in jobs:
            jid = str(j.get("jobId") or j.get("reqId") or "").strip()
            if not jid:
                continue
            loc = (j.get("cityStateCountry") or j.get("location") or "").strip()
            if j.get("isMultiLocation") and len(j.get("multi_location") or []) > 1:
                loc = f"{loc} (+{len(j['multi_location']) - 1} more)"
            if "remote" in (j.get("applyUrl") or "").lower() and "remote" not in loc.lower():
                loc = (loc + " (Remote)").strip() if loc else "Remote"
            out.append(_norm(c, jid, j.get("title", ""), loc, j.get("applyUrl", ""),
                             (j.get("postedDate") or j.get("dateCreated") or "")[:10],
                             ats="phenom", description=_clean_html(j.get("descriptionTeaser", ""))))
        frm += size
        if frm >= total or not jobs:
            break
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday,
            "ashby": fetch_ashby, "recruitee": fetch_recruitee, "personio": fetch_personio,
            "aquent": fetch_aquent, "snaphop": fetch_snaphop, "phenom": fetch_phenom}


def _norm(company, ext_id, title, location, url, posted,
          salary=None, ats=None, detail_url=None, description=""):
    # `posted` = best "first posted" date the list endpoint gives (YYYY-MM-DD or "").
    # `salary` = pay known for free at list time (Lever); else filled by enrich_salary().
    # `description` feeds LLM scoring + salary regex. `_ats`/`_detail_url` are private
    # (underscore-prefixed) and stripped, along with `description`, before a snapshot is written.
    return {"key": f"{company['name']}::{ext_id}", "company": company["name"],
            "title": title.strip(), "location": location.strip(),
            "url": url, "posted": posted, "salary": salary, "description": description,
            "_ats": ats, "_detail_url": detail_url}


def _matches_any(l, terms):
    # Word-boundary match (like matches_profile) so short tokens ("ut", "uk") don't hit
    # inside unrelated words — e.g. "So[ut]hampton" was wrongly read as Utah-local.
    return any(re.search(r"\b" + re.escape(t) + r"\b", l) for t in terms)


def is_local(loc):
    l = loc.lower()
    # Remote roles pass — unless a non-US country is named and international remote is off,
    # in which case fall through to the keyword check (kept only if it ALSO names a local city).
    if KEEP_REMOTE and "remote" in l:
        if ALLOW_INTL_REMOTE or not _matches_any(l, INTERNATIONAL_MARKERS):
            return True
    return _matches_any(l, LOCAL_KEYWORDS)


def is_us_remote(loc):
    """Gate for the US-remote lane: keep a role only if its location is REMOTE and
    US-eligible. Drops location-locked roles (must name a remote marker) and non-US
    remote roles (names a non-US country, unless allow_international_remote). A generic
    'Remote'/'Anywhere' with no country named is treated as US-eligible."""
    l = (loc or "").lower()
    if not _matches_any(l, ["remote", "anywhere", "distributed", "work from home", "wfh", "virtual"]):
        return False
    if _matches_any(l, ["us", "usa", "united states", "u.s", "u.s.", "americas", "north america"]):
        return True
    if _matches_any(l, INTERNATIONAL_MARKERS):
        return ALLOW_INTL_REMOTE
    return True


# ---- posting-date + salary enrichment (Lever inline; others: description/detail regex) ----

_INTERVAL = {"per-year-salary": "/yr", "per-hour-wage": "/hr",
             "per-month-salary": "/mo", "per-week-salary": "/wk", "per-day-wage": "/day"}


def _fmt_pay(lo, hi, currency, interval):
    sym = "$" if currency in (None, "USD") else f"{currency} "
    unit = _INTERVAL.get(interval, "")
    fmt = lambda n: f"{sym}{n:,.0f}" if float(n) >= 1000 else f"{sym}{float(n):,.2f}"
    return f"{fmt(lo)}–{fmt(hi)}{unit}"


# A "$X – $Y" pay range in free text: two dollar amounts joined by a dash/"to".
_PAY_RE = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?[kK]?"          # first amount
    r"\s*(?:-|–|—|to)\s*"                                 # separator: - – — or "to"
    r"\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?[kK]?")        # second amount

_SALARY_CACHE = {}   # key -> salary string|None, so a role shared across profiles is fetched once


def _detail_text(p):
    # Plain-text job description from each ATS's per-posting detail endpoint (salary fallback).
    d, ats = _get(p["_detail_url"]), p["_ats"]
    if ats == "smartrecruiters":
        secs = (d.get("jobAd") or {}).get("sections") or {}
        text = " ".join(v.get("text", "") for v in secs.values() if isinstance(v, dict))
        return _clean_html(text, limit=20000)
    if ats == "workday":
        return _clean_html((d.get("jobPostingInfo") or {}).get("jobDescription", ""), limit=20000)
    return _clean_html(d.get("content", ""), limit=20000)   # greenhouse


def enrich_salary(postings):
    # Fill missing salary: regex the description we already fetched; only hit the detail
    # endpoint when there's no description (SmartRecruiters/Workday are title-only). Cached
    # by key so a role shared across profiles costs at most one detail fetch. Bounded to
    # the matched roles passed in — never the whole pool.
    for p in postings:
        if p.get("salary"):
            continue
        if p["key"] not in _SALARY_CACHE:
            try:
                text = p.get("description") or ""
                if not text and p.get("_detail_url"):
                    text = _detail_text(p)
                m = _PAY_RE.search(text or "")
                _SALARY_CACHE[p["key"]] = m.group(0).strip() if m else None
            except Exception:
                _SALARY_CACHE[p["key"]] = None
        p["salary"] = _SALARY_CACHE[p["key"]]


def _fmt_posted(posted):
    # "Posted Jul 10 · 3d ago" from a YYYY-MM-DD string; "" if absent/unparseable.
    if not posted:
        return ""
    try:
        d = datetime.date.fromisoformat(posted)
    except ValueError:
        return ""
    days = (datetime.date.today() - d).days
    age = "today" if days <= 0 else "1d ago" if days == 1 else f"{days}d ago"
    return f"Posted {d:%b} {d.day} · {age}"


def _is_fresh(posted):
    # True if the posting is within STAR_WITHIN_DAYS (0/null or unknown date → not fresh).
    if not STAR_WITHIN_DAYS or not posted:
        return False
    try:
        d = datetime.date.fromisoformat(posted)
    except ValueError:
        return False
    return (datetime.date.today() - d).days <= STAR_WITHIN_DAYS


# ---- profile matching (word-boundary aware) ----

def _any_term(title, terms):
    return any(re.search(r"\b" + re.escape(t) + r"\b", title) for t in terms)


def _mandate_rescue(posting, profile):
    """Second chance for a role whose TITLE misses `match_groups` but whose DESCRIPTION shows
    the mandate we're actually looking for — the "generically titled but highly relevant"
    case (several of Lisa's own past roles were titled that way).

    Opt-in per profile via `mandate_rescue`:
        {"require_title_any": [...],   # must still read as a leadership role
         "terms": [...],               # mandate vocabulary to look for in the description
         "min_hits": 2}                # how many DISTINCT terms must appear

    Deliberate limits:
      * Exclusions are checked by the caller BEFORE this runs, so a rescue can never drag
        back an accounting/engineering/clinical role.
      * `require_title_any` keeps the rescue anchored to leadership-shaped titles instead of
        letting any description keyword through.
      * Distinct terms are counted, so one phrase repeated in boilerplate can't rescue alone.
      * **No description, no rescue.** SmartRecruiters and Workday are title-only at list
        time (11 of the local companies), so generically-titled roles there stay invisible.
        This is a known coverage gap, not an oversight — see PROSPECTOR_V2_CHANGELOG.md.
    A rescued role is kept for LLM scoring to judge; the rescue widens recall, the model
    supplies the precision."""
    cfg = profile.get("mandate_rescue") or {}
    terms = cfg.get("terms") or []
    if not terms:
        return False
    title = posting["title"].lower()
    gate = cfg.get("require_title_any") or []
    if gate and not _any_term(title, gate):
        return False
    desc = (posting.get("description") or "").lower()
    if not desc:
        return False
    hits = sum(1 for t in terms if re.search(r"\b" + re.escape(t) + r"\b", desc))
    if hits < int(cfg.get("min_hits", 2)):
        return False
    # Mark WHY this role is here, per profile (postings are shared across profiles).
    # Underscore-prefixed, so it is stripped before the snapshot is written.
    posting.setdefault("_rescued_for", set()).add(profile.get("name"))
    return True


def matches_profile(posting, profile):
    """Keep a posting iff its title matches NONE of `exclude_any` AND at least one term in
    EVERY `match_groups` entry (AND across groups, OR within) — or, failing the title test,
    its description carries the mandate (see `_mandate_rescue`).

    Matching is word-boundary aware on purpose so short tokens ("coo", "vp") don't match
    inside longer words ("coordinator", "improve"). Preserve that behavior."""
    title = posting["title"].lower()
    if _any_term(title, profile.get("exclude_any", [])):
        return False                       # exclusions are absolute and title-only
    if all(_any_term(title, g) for g in profile.get("match_groups", [])):
        return True
    return _mandate_rescue(posting, profile)


# ---- LLM fit scoring (optional) ----

def get_client():
    """Return an Anthropic client if a key is set and the SDK is installed, else None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    except ImportError:
        print("[warn] ANTHROPIC_API_KEY set but 'anthropic' package not installed; skipping fit scoring.")
        return None


SIMULATED_SCORING = False   # True only under --fake-fit; drives a warning banner in reports


class SimulatedFitClient:
    """`--fake-fit` only. Generates DETERMINISTIC pseudo-verdicts locally so the email layout
    can be reviewed without an API key and without spending anything. Same call surface as
    the real client, so it exercises the genuine score_fit + validate_verdict path.

    Every report produced this way carries a loud banner — simulated scores must never be
    mistaken for real judgments."""

    class _Block:
        def __init__(self, text):
            self.type, self.text = "text", text

    class _Message:
        def __init__(self, text):
            self.content = [SimulatedFitClient._Block(text)]
            self.usage = None          # no real tokens were spent

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        user = kw.get("messages", [{}])[0].get("content", "")
        h = int(hashlib.md5(user.encode("utf-8")).hexdigest(), 16)
        qual, interest = 45 + h % 55, 40 + (h >> 8) % 60
        practical = 100 if "remote" in user.lower() else 45 + (h >> 16) % 40
        opp = (qual + interest + practical) // 3
        if "contract or temporary" in user:
            rec = "practical_contract"
        elif opp >= 82:
            rec = "apply_first"
        elif opp >= 70:
            rec = "strong_fit"
        elif opp >= 45:
            rec = "stretch"
        else:
            rec = "not_recommended"
        return self._Message(json.dumps({
            "qualification_fit": qual, "interest_fit": interest,
            "practical_fit": practical, "opportunity_score": opp,
            "recommendation": rec,
            "reasons": ["SIMULATED score — layout preview only",
                        "no Anthropic API call was made"],
            "concerns": ["Run without --fake-fit for real judgments"],
            "relocation_required": practical < 55,
            "relocation_assistance_mentioned": False,
            "signing_bonus_mentioned": False}))


def _simulated_banner():
    if not SIMULATED_SCORING:
        return ""
    return (f'<div style="background-color:{_C["red_bg"]};border:1px solid {_C["red"]};'
            f'color:{_C["red"]};font-family:{_FONT};font-size:13px;font-weight:700;'
            f'padding:10px 14px;border-radius:8px;margin:12px 0;">'
            f'⚠️ SIMULATED SCORES — generated with --fake-fit for layout preview. '
            f'No Anthropic API call was made and none of these numbers are real.</div>')


def load_background(profile):
    f = profile.get("background_file")
    if not f:
        return None
    try:
        return json.load(open(os.path.join(HERE, f)))
    except Exception as e:
        print(f"[warn] could not load background_file '{f}': {type(e).__name__}")
        return None


def _bg_fingerprint(candidate):
    # Short hash of what is actually sent to the model, PLUS the verdict-schema version.
    # Editing a background file changes this (invalidating that profile's cached verdicts),
    # and so does bumping FIT_SCHEMA_VERSION — which is what guarantees a stale
    # old-shape verdict is never reused by new rendering code. See enrich_with_fit.
    payload = {"v": FIT_SCHEMA_VERSION, "bg": candidate}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


# recommendation -> the legacy `fit` bucket, so everything written against the old verdict
# shape (Chad's report rendering, the `fit_mode:"filter"` switch, score sorting) keeps working.
_REC_TO_FIT = {"apply_first": "yes", "strong_fit": "yes", "stretch": "maybe",
               "practical_contract": "maybe", "not_recommended": "no"}
RECOMMENDATIONS = tuple(_REC_TO_FIT)


def _neutral_verdict(why):
    """The safe fallback. score -1 marks it UNCACHEABLE, so the role is kept in the report
    and re-scored on the next run rather than being silently frozen as a bad verdict."""
    return {"recommendation": "stretch", "fit": "maybe", "score": -1,
            "opportunity_score": -1, "qualification_fit": -1, "interest_fit": -1,
            "practical_fit": -1, "reasons": [], "concerns": [], "reason": why,
            "relocation_required": False, "relocation_assistance_mentioned": False,
            "signing_bonus_mentioned": False, "valid": False}


def _as_score(value):
    """0-100 int, or None if the model sent something that isn't a number. Bools are
    rejected on purpose (True would otherwise coerce to 1)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _as_str_list(value, limit, item_chars=140):
    if isinstance(value, str):          # model sent one string instead of a list
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        s = str(item).strip()
        if s:
            out.append(s[:item_chars])
    return out


def validate_verdict(raw):
    """Turn a parsed model reply into a trusted verdict, or None if it can't be trusted.

    Strict about the fields that drive decisions (the four scores + recommendation): if any
    is missing or malformed the whole verdict is rejected, because a wrong recommendation is
    worse than no recommendation. Forgiving about presentation (over-long lists, a string
    where a list belongs, non-bool booleans) — those are coerced."""
    if not isinstance(raw, dict):
        return None
    rec = str(raw.get("recommendation", "")).strip().lower().replace("-", "_").replace(" ", "_")
    if rec not in _REC_TO_FIT:
        return None
    scores = {k: _as_score(raw.get(k)) for k in
              ("qualification_fit", "interest_fit", "practical_fit", "opportunity_score")}
    if any(v is None for v in scores.values()):
        return None
    reasons = _as_str_list(raw.get("reasons"), 5)
    v = {"recommendation": rec,
         "reasons": reasons,
         "concerns": _as_str_list(raw.get("concerns"), 3),
         "relocation_required": bool(raw.get("relocation_required")),
         "relocation_assistance_mentioned": bool(raw.get("relocation_assistance_mentioned")),
         "signing_bonus_mentioned": bool(raw.get("signing_bonus_mentioned")),
         "valid": True}
    v.update(scores)
    # Legacy aliases, kept so existing renderers/sorting/filter logic need no changes.
    v["score"] = scores["opportunity_score"]
    v["fit"] = _REC_TO_FIT[rec]
    v["reason"] = reasons[0] if reasons else ""
    return v


# Built once per run (it is identical for every posting) and prepended to each request.
def _fit_instructions():
    return (
        "You screen job postings for ONE specific candidate and return a structured "
        "judgment. Be realistic and decisive.\n\n"
        "Score four independent dimensions 0-100:\n"
        "- qualification_fit: how well the candidate's demonstrated experience matches the "
        "role's actual requirements and mandate.\n"
        "- interest_fit: how strongly the role matches the work they want — transformation, "
        "organizational strategy, operations, operational excellence, program leadership, "
        "customer or employee experience, change, professional services, strategic "
        "initiatives, AI adoption and enablement, M&A integration, operating models, "
        "value realization.\n"
        "- practical_fit: how workable it is — remote status, Utah location, hybrid or "
        "onsite requirements, whether relocation would be needed, employment type, and "
        "compensation or other constraints the posting actually states.\n"
        "- opportunity_score: the overall value of PURSUING this role, balancing the three "
        "above. This is not resume similarity — a strong mandate at a real company can "
        "outrank a closer keyword match.\n\n"
        "Location weighting for practical_fit: US-remote is best; Utah-local or Utah-hybrid "
        "is high; hybrid outside Utah is reduced; onsite outside Utah is reduced because it "
        "would likely require relocating. A weak practical_fit must NOT erase a strong role "
        "— score it honestly and let the labels carry the caveat.\n\n"
        "Do NOT automatically penalize a role for being individual-contributor, Lead, "
        "Principal, Manager, Senior Manager, Director, contract, or temporary. Judge whether "
        "it carries real ownership, influence, strategic scope, transformation or operating "
        "responsibility, or executive-facing work. Many strong roles are generically titled, "
        "so weight the mandate and problems described over title keywords.\n\n"
        "recommendation must be exactly one of:\n"
        "- apply_first: well qualified, strongly matches their interests, practical enough "
        "to prioritize now.\n"
        "- strong_fit: credible and attractive, worth serious consideration.\n"
        "- stretch: interesting and possibly viable, but with real gaps, seniority concerns, "
        "specialized requirements, or practical limits.\n"
        "- practical_contract: a contract or temporary role useful for income or experience "
        "even if it is not a long-term ideal.\n"
        "- not_recommended: wrong function, unrealistic requirements, weak mandate fit, or "
        "otherwise not worth their time.\n\n"
        "relocation_assistance_mentioned and signing_bonus_mentioned must be true ONLY if "
        "the posting explicitly says so. Never infer them. If the description is missing or "
        "silent, they are false.\n\n"
        "reasons: up to 5 items, each a short phrase (under 15 words) explaining why this "
        "role surfaced. concerns: up to 3, only if genuinely useful.\n\n"
        "Respond with ONLY this JSON object — no prose, no markdown fences:\n"
        '{"qualification_fit": <0-100>, "interest_fit": <0-100>, "practical_fit": <0-100>, '
        '"opportunity_score": <0-100>, "recommendation": "apply_first"|"strong_fit"|'
        '"stretch"|"practical_contract"|"not_recommended", "reasons": ["..."], '
        '"concerns": ["..."], "relocation_required": true|false, '
        '"relocation_assistance_mentioned": true|false, '
        '"signing_bonus_mentioned": true|false}')


def _fit_system(candidate):
    """The STABLE half of the prompt: scoring rubric + candidate profile. Byte-identical for
    every posting in a run, so it is sent as a cached system block — the first call writes the
    cache, the rest read it at roughly a tenth of the input price. `sort_keys=True` matters:
    non-deterministic JSON ordering would silently change the bytes and defeat the cache.

    A cache miss is harmless — it just costs what it did before, so this is a saving, not a
    dependency. Nothing downstream inspects the cache fields."""
    text = (_fit_instructions() + "\n\nCANDIDATE:\n"
            + json.dumps(candidate, indent=2, sort_keys=True))
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# Per-run token accounting, printed at the end of a scored run so cost stays visible.
_FIT_USAGE = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _record_usage(msg):
    u = getattr(msg, "usage", None)
    if u is None:                      # test doubles won't carry usage; not an error
        return
    _FIT_USAGE["calls"] += 1
    for key, attr in (("input", "input_tokens"), ("output", "output_tokens"),
                      ("cache_read", "cache_read_input_tokens"),
                      ("cache_write", "cache_creation_input_tokens")):
        _FIT_USAGE[key] += getattr(u, attr, 0) or 0


def fit_usage_summary():
    """One-line cost/caching summary, or '' if nothing was scored."""
    u = _FIT_USAGE
    if not u["calls"]:
        return ""
    billed = u["input"] + u["cache_write"] + u["cache_read"]
    hit = (100 * u["cache_read"] // billed) if billed else 0
    return (f"Fit scoring: {u['calls']} call(s) · in {u['input']:,} · "
            f"cache write {u['cache_write']:,} · cache read {u['cache_read']:,} "
            f"({hit}% of prompt served from cache) · out {u['output']:,}")


def score_fit(candidate, posting, client):
    """Score one posting for one candidate. Returns a validated verdict dict (see
    `validate_verdict`) carrying the four dimensions, a recommendation, reasons/concerns,
    the three explicit-mention booleans, and legacy `fit`/`score`/`reason` aliases.

    Never raises. On ANY failure — API error, unparseable reply, schema that fails
    validation — returns a neutral verdict with score -1, so the role is KEPT and re-scored
    next run instead of being dropped or frozen with a wrong recommendation."""
    desc = posting.get("description") or \
        "(no description available — judge from title and location only)"
    # Only the posting varies per call, so it is the whole user turn (see _fit_system).
    user = (f"JOB POSTING:\nTitle: {posting['title']}\nCompany: {posting['company']}\n"
            f"Location: {posting['location']}\n"
            f"Employment type hint: {posting.get('_lane') or 'standard posting'}\n"
            f"Description: {desc[:DESC_LIMIT]}")
    text = ""
    try:
        # max_tokens generous so reasons/concerns can't truncate the JSON mid-string.
        msg = client.messages.create(model=FIT_MODEL, max_tokens=700,
                                     system=_fit_system(candidate),
                                     messages=[{"role": "user", "content": user}])
        _record_usage(msg)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        # Pull the JSON object out even if the model wrapped it in fences or prose.
        m = re.search(r"\{.*\}", text, re.S)
        verdict = validate_verdict(json.loads(m.group(0) if m else text))
        if verdict is None:
            print(f"[warn] fit schema rejected [{posting.get('key', '?')}]: "
                  f"raw={text[:160]!r}")
            return _neutral_verdict("(scoring unavailable: schema validation failed)")
        return verdict
    except Exception as e:
        print(f"[warn] fit parse failed [{posting.get('key', '?')}]: "
              f"{type(e).__name__}: {str(e)[:80]} | raw={text[:120]!r}")
        return _neutral_verdict(f"(scoring unavailable: {type(e).__name__})")


def enrich_with_fit(matched, prev, profile, client):
    """Attach fit_result to each posting. Reuse a cached verdict only when it scored
    successfully (score >= 0) AND was produced against the SAME background (its stored
    `bg` fingerprint matches the current one). Editing the background_file changes the
    fingerprint, so every role is re-scored on the next run — no manual cache clearing.
    Verdicts predating this feature carry no `bg`, so they also re-score once."""
    candidate = load_background(profile)
    if not (candidate and client):
        return 0
    fp = _bg_fingerprint(candidate)
    cached = {p["key"]: p["fit_result"] for p in (prev or [])
              if (p.get("fit_result") or {}).get("score", -1) >= 0
              and (p.get("fit_result") or {}).get("bg") == fp}
    scored = 0
    for p in matched:
        if p["key"] in cached:
            p["fit_result"] = cached[p["key"]]
        else:
            r = score_fit(candidate, p, client)
            r["bg"] = fp                       # stamp the background it was scored against
            p["fit_result"] = r
            scored += 1
    return scored


# ---- pipeline ----

def _within_age(posted, max_age_days):
    # Keep if there's no age limit, or the date is unknown (never drop what we can't date),
    # or it's within the window. Only a confidently-too-old posting is dropped.
    if not max_age_days or not posted:
        return True
    try:
        d = datetime.date.fromisoformat(posted)
    except ValueError:
        return True
    return (datetime.date.today() - d).days <= max_age_days


def collect_pool(max_age_days=None, config_path=CONFIG, gate=None):
    """Fetch + normalize every source in `config_path` once, apply a geography `gate`
    (defaults to the local gate `is_local` when None), then the age gate. The remote lane
    calls this with config_path=REMOTE_CONFIG and gate=is_us_remote.

    Returns (pool, errors, failed_companies). `failed_companies` is the set of company names
    whose fetch raised — those companies contributed NOTHING to the pool, so the caller must
    hold their previously-known roles out of the removal diff (see `_run_lane`) instead of
    reporting them as filled."""
    if not os.path.exists(config_path):
        return [], [], set()
    cfg = json.load(open(config_path))
    keep = gate or (is_local if LOCAL_ONLY else None)
    pool, errors, failed = [], [], set()
    for c in cfg.get("companies", []):
        try:
            got = FETCHERS[c["ats"]](c)
            if keep:
                got = [p for p in got if keep(p["location"])]
            got = [p for p in got if _within_age(p["posted"], max_age_days)]
            pool.extend(got)
        except Exception as e:
            errors.append(f"{c['name']} ({c['ats']}/{c['slug']}): {type(e).__name__} {e}")
            failed.add(c["name"])
    return pool, errors, failed


# ---- feedback (WS5): the smallest thing that controls repetition ----------------------
#
# One committed JSON file per profile: feedback_<name>.json. Hand-edited; no UI.
# An entry may identify a role by its exact `key`, or — because keys are unfriendly to type —
# by `company` + `title` (case-insensitive, trimmed). See PROSPECTOR_TESTING.md.
#
# Statuses and what each one DOES (per Lisa's spec):
#   applied         -> never recommended again; appears under Hiring Progress once it closes
#   already_applied -> suppressed from recommendations
#   not_interested  -> suppressed permanently
#   too_technical   -> suppressed; counted as a false positive for the weekly audit
#   wrong_function  -> suppressed; counted as a false positive
#   wrong_industry  -> suppressed; counted as a false positive
#   interested      -> keeps showing until it closes or ages out (NOT suppressed)
FEEDBACK_STATUSES = ("applied", "already_applied", "not_interested", "too_technical",
                     "wrong_function", "wrong_industry", "interested")
# Statuses that remove a role from the recommendation sections.
SUPPRESS_STATUSES = ("applied", "already_applied", "not_interested", "too_technical",
                     "wrong_function", "wrong_industry")
# Statuses that mean "this was a bad recommendation" — the audit's false-positive signal.
FALSE_POSITIVE_STATUSES = ("too_technical", "wrong_function", "wrong_industry",
                           "not_interested")


def _fb_ident(company, title):
    return f"{(company or '').strip().lower()}::{(title or '').strip().lower()}"


def load_feedback(profile_name, directory=None):
    """Return {"by_key": {...}, "by_ident": {...}} of status records. A missing, empty or
    malformed file is never fatal — feedback is an enhancement, not a dependency."""
    path = os.path.join(directory or HERE, f"feedback_{profile_name}.json")
    out = {"by_key": {}, "by_ident": {}}
    if not os.path.exists(path):
        return out
    try:
        raw = json.load(open(path))
    except Exception as e:
        print(f"[warn] feedback_{profile_name}.json unreadable ({type(e).__name__}); ignoring.")
        return out
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        status = str(e.get("status", "")).strip().lower()
        if status not in FEEDBACK_STATUSES:
            if status:
                print(f"[warn] feedback_{profile_name}.json: unknown status "
                      f"{status!r} (allowed: {', '.join(FEEDBACK_STATUSES)}); ignoring entry.")
            continue
        rec = {"status": status, "note": str(e.get("note", "")).strip(),
               "date": str(e.get("date", "")).strip()}
        if e.get("key"):
            out["by_key"][str(e["key"]).strip()] = rec
        if e.get("company") and e.get("title"):
            out["by_ident"][_fb_ident(e["company"], e["title"])] = rec
    return out


def feedback_for(posting, feedback):
    """The status record for a posting, matched by key first then company+title."""
    if not feedback:
        return None
    return (feedback["by_key"].get(posting.get("key"))
            or feedback["by_ident"].get(_fb_ident(posting.get("company"),
                                                  posting.get("title"))))


# ---- removal classification (WS4) -----------------------------------------------------
#
# The engine genuinely cannot tell "filled" from "pulled" — so it never claims either.
# Two of the four causes ARE determinable from data we already hold, and a third (source
# error) is handled upstream in _run_lane by holding those roles out of the diff entirely.
REMOVAL_REASONS = {
    "aged_out":      "Aged past the display window (may still be open)",
    "filter_change": "No longer matches the current search rules",
    "not_listed":    "Posting no longer detected at the source",
}


def classify_removal(prev_role, profile, max_age_days):
    """Why did this role leave the pool? Cautious by design: only claims what it can prove,
    and otherwise says the posting is no longer *detected* rather than filled."""
    if max_age_days and not _within_age(prev_role.get("posted", ""), max_age_days):
        return "aged_out"
    # Re-run the title gate against the stored record. A previously RESCUED role can't be
    # re-checked (snapshots carry no description), so `_title_gate_only` is used and a rescue
    # is never mistaken for a rule change.
    if not _title_gate_only(prev_role, profile) and not prev_role.get("rescued"):
        return "filter_change"
    return "not_listed"


def _title_gate_only(role, profile):
    """matches_profile without the description-based rescue (snapshots have no description)."""
    title = (role.get("title") or "").lower()
    if _any_term(title, profile.get("exclude_any", [])):
        return False
    return all(_any_term(title, g) for g in profile.get("match_groups", []))


def diff(prev, curr):
    pmap, cmap = {p["key"]: p for p in prev}, {p["key"]: p for p in curr}
    new     = [cmap[k] for k in cmap if k not in pmap]
    removed = [pmap[k] for k in pmap if k not in cmap]
    changed = [(pmap[k], cmap[k]) for k in cmap
               if k in pmap and pmap[k]["title"] != cmap[k]["title"]]
    return new, removed, changed


def _fit_badge(p):
    fr = p.get("fit_result")
    if not fr:
        return ""
    s = fr.get("score", -1)
    return f" — **{s}/100** ({fr.get('fit','?')})" if s >= 0 else f" — _{fr.get('reason','')}_"


def _fit_reason(p):
    fr = p.get("fit_result")
    return f"\n   _{fr['reason']}_" if fr and fr.get("reason") and fr.get("score", -1) >= 0 else ""


def _star(p):
    # Leading "⭐ " for a posting newer than STAR_WITHIN_DAYS, else "".
    return "⭐ " if _is_fresh(p.get("posted")) else ""


def _meta_md(p):
    # "location · salary · Posted …" — the detail suffix after a role's title.
    bits = [p.get("location") or ""]
    if p.get("salary"):
        bits.append(p["salary"])
    fp = _fmt_posted(p.get("posted"))
    if fp:
        bits.append(fp)
    return " · ".join(b for b in bits if b)


def _md_lane(lane):
    # Markdown for one lane's sections (What's changed / All current / warnings).
    matched, new, removed, changed = lane["matched"], lane["new"], lane["removed"], lane["changed"]
    errors, first_run = lane["errors"], lane["first_run"]
    scored = any(p.get("fit_result") for p in matched)
    L = ["## What's changed"]
    if first_run:
        L.append("_First run — baseline established. Changes will appear here on the next run._")
    elif not (new or changed):   # removed/filled is rendered in its own bottom section
        L.append("_No new or changed roles since the previous run._")
    else:
        if new:
            L.append(f"**New ({len(new)})**")
            order = sorted(new, key=lambda x: -(x.get("fit_result") or {}).get("score", 0)) if scored \
                else sorted(new, key=lambda x: x["company"])
            for p in order:
                L.append(f"- {_star(p)}**{p['company']}** — [{p['title']}]({p['url']}) · {_meta_md(p)}{_fit_badge(p)}{_fit_reason(p)}")
        if changed:
            L.append(f"**Changed titles ({len(changed)})**")
            L += [f"- **{c['company']}** — \"{o['title']}\" → [{c['title']}]({c['url']})"
                  for o, c in changed]
    L.append("")
    L.append(f"## All current matching roles ({len(matched)})")
    if not matched:
        L.append("_No roles currently match this profile._")
    elif scored:
        for p in sorted(matched, key=lambda x: -(x.get("fit_result") or {}).get("score", 0)):
            L.append(f"- {_star(p)}**{p['company']}** — [{p['title']}]({p['url']}) · {_meta_md(p)}{_fit_badge(p)}{_fit_reason(p)}")
    else:
        last = None
        for p in sorted(matched, key=lambda x: (x["company"], x["title"])):
            if p["company"] != last:
                L.append(f"\n**{p['company']}**")
                last = p["company"]
            L.append(f"- {_star(p)}[{p['title']}]({p['url']}) · {_meta_md(p)}")
    L.append("")
    if errors:
        L.append(f"## Source warnings ({len(errors)})")
        L += [f"- {e}" for e in errors]
        L.append("")
    return L


def build_report(profile, lanes):
    # `lanes` is a list of lane dicts; rendered under one header. A single lane (remote off)
    # renders exactly as before; two lanes get a `# <lane title>` heading each.
    today = datetime.date.today().isoformat()
    L = [f"# {profile['label']}", f"### Job report — {today}"]
    if STAR_WITHIN_DAYS:
        L.append(f"_⭐ = posted in the last {STAR_WITHIN_DAYS} days_")
    c = _digest_counts(lanes)   # one-line digest mirroring the email's preheader/hero summary
    if c["new"]:
        seg = [f'{_lane_short(l["title"])[0]} {len(l["new"])} {_lane_short(l["title"])[1]}'
               for l in lanes if l["new"]]
        L.append(f'\n**{len(c["new"])} new today** · ' + " · ".join(seg))
    elif c["first_run"]:
        L.append(f'\n**Baseline established** · {c["matched"]} roles tracked')
    multi = len(lanes) > 1
    for lane in lanes:
        L.append("")
        if multi:
            L.append(f"# {lane['title']} ({len(lane['matched'])})")
        L += _md_lane(lane)
    # Removed / filled roles collected at the very bottom, one sub-section per lane.
    removed_lanes = [lane for lane in lanes if lane["removed"]]
    if removed_lanes:
        L.append("")
        L.append("# Removed / filled")
        for lane in removed_lanes:
            L.append("")
            L.append(f"## {lane['title']} ({len(lane['removed'])})")
            L += [f"- **{p['company']}** — {p['title']} · {_meta_md(p)}"
                  for p in sorted(lane["removed"], key=lambda x: x["company"])]
    return "\n".join(L)


# ---- HTML report (dark-mode, email-safe: inline styles, table layout) ----

# GitHub-dark palette. Explicit 6-digit hex so mail clients need no color blending.
_C = {
    "bg": "#0d1117", "card": "#161b22", "panel": "#1c2128", "border": "#30363d",
    "text": "#c9d1d9", "head": "#f0f6fc", "muted": "#8b949e", "link": "#58a6ff",
    "green": "#3fb950", "amber": "#d29922", "red": "#f85149",
    "green_bg": "#122619", "amber_bg": "#2b2411", "red_bg": "#2d1618",
    "link_bg": "#12233a", "muted_bg": "#1c2128",
}
# Inter (the modern web font) first, then a system-sans fallback stack. Clients that
# support web fonts (Apple Mail, iOS) render Inter; those that don't (Gmail, Outlook)
# fall back to the system sans — either way it's ONE consistent sans-serif, never serif.
_FONT = ("'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "Helvetica,Arial,sans-serif")
_FIT_COLOR = {"yes": "green", "maybe": "amber", "no": "red"}

# Company logos are prefetched into logos/<slug>.png by fetch_logos.py and embedded in
# the email as inline CID attachments (<img src="cid:<slug>.png">) — which render inline
# in Gmail/Apple Mail/Outlook without a runtime fetch. When a logo file is absent we fall
# back to a colored monogram of the company's initials. _LOGOS_USED collects the files
# referenced while building one report so the workflow can attach exactly those (an
# attached-but-unreferenced file would show as a stray download). Slugs come from
# companies.json (loaded once, lazily).
LOGO_DIR = os.path.join(HERE, "logos")
_COMPANIES = None
_LOGOS_USED = set()
_MONO_COLORS = ["#1f6feb", "#238636", "#8957e5", "#bb8009", "#c93c37",
                "#0e7490", "#a21caf", "#2da44e"]


def _company_slug(company):
    global _COMPANIES
    if _COMPANIES is None:
        _COMPANIES = {}
        for path in (CONFIG, REMOTE_CONFIG, STAFFING_CONFIG):  # local + US-remote + staffing
            try:
                for c in json.load(open(path)).get("companies", []):
                    _COMPANIES.setdefault(c["name"], c)
            except Exception:
                pass
    return (_COMPANIES.get(company) or {}).get("slug")


def _initials(company):
    words = [w for w in re.split(r"[^A-Za-z0-9]+", company) if w]
    if not words:
        return "?"
    return (words[0][:2] if len(words) == 1 else words[0][0] + words[1][0]).upper()


def _mono_color(company):
    h = int(hashlib.md5(company.encode("utf-8")).hexdigest(), 16)
    return _MONO_COLORS[h % len(_MONO_COLORS)]


def _logo_square(company):
    # Fixed 40px rounded tile shown to the left of a posting: a prefetched logo on a
    # white tile (inline CID) when logos/<slug>.png exists, else a colored monogram.
    initials = _esc(_initials(company))
    slug = _company_slug(company)
    if slug and os.path.exists(os.path.join(LOGO_DIR, f"{slug}.png")):
        _LOGOS_USED.add(f"logos/{slug}.png")
        # logo.dev icons are full-bleed squares with their own background, so fill the
        # tile edge-to-edge; the white cell shows only behind a transparent logo.
        cell = (f'<td align="center" valign="middle" style="width:64px;height:64px;'
                f'background-color:#ffffff;border-radius:12px;">'
                f'<img src="cid:{slug}.png" width="64" height="64" alt="{initials}" '
                f'style="display:block;border:0;border-radius:12px;"></td>')
    else:
        cell = (f'<td align="center" valign="middle" style="width:64px;height:64px;'
                f'background-color:{_mono_color(company)};border-radius:12px;color:#ffffff;'
                f'font-family:{_FONT};font-size:24px;font-weight:700;">{initials}</td>')
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="64" style="width:64px;"><tr>{cell}</tr></table>')


def _icon_row(company, content):
    # Two-column row: logo square on the left, posting content on the right. The left
    # column hugs the tile (width = tile + a small gap) so the logo sits tight to the text.
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'width="100%" style="width:100%;"><tr>'
            '<td valign="top" width="70" style="width:70px;padding-right:6px;">'
            f'{_logo_square(company)}</td>'
            f'<td valign="top">{content}</td></tr></table>')


def _esc(s):
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _link(text, url):
    return (f'<a href="{_esc(url)}" style="color:{_C["link"]};text-decoration:none;'
            f'font-family:{_FONT};font-weight:600;">{_esc(text)}</a>')


def _muted(text):
    return (f'<div style="color:{_C["muted"]};font-family:{_FONT};font-size:13px;'
            f'margin-top:4px;">{text}</div>')


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


def _fit_pill_html(p):
    # Colored score pill (green/amber/red by verdict); empty when score unavailable.
    fr = p.get("fit_result") or {}
    s = fr.get("score", -1)
    if s < 0:
        return ""
    key = _FIT_COLOR.get(fr.get("fit", ""), "amber")
    return (f'<span style="display:inline-block;background-color:{_C[key + "_bg"]};'
            f'color:{_C[key]};font-family:{_FONT};font-size:12px;font-weight:700;'
            f'padding:2px 9px;border-radius:10px;margin-left:8px;white-space:nowrap;">'
            f'{s}/100 · {_esc(fr.get("fit", "?"))}</span>')


def _fit_reason_html(p):
    fr = p.get("fit_result") or {}
    return _muted(_esc(fr["reason"])) if fr.get("reason") else ""


def _meta_html(p, lead=None):
    # Muted "lead · location · Posted …" line, with salary called out in green beneath.
    parts = [_esc(lead)] if lead else []
    if p.get("location"):
        parts.append(_esc(p["location"]))
    fp = _fmt_posted(p.get("posted"))
    if fp:
        parts.append(_esc(fp))
    line = _muted(" · ".join(parts)) if parts else ""
    if p.get("salary"):
        line += (f'<div style="color:{_C["green"]};font-family:{_FONT};font-size:13px;'
                 f'font-weight:700;margin-top:3px;">{_esc(p["salary"])}</div>')
    return line


def _star_html(p):
    # Leading star for a posting newer than STAR_WITHIN_DAYS, else "".
    return '<span style="font-size:14px;">⭐</span> ' if _is_fresh(p.get("posted")) else ""


def _role_inner(p, lead=None):
    # Title (+ star + fit pill) on one line; muted meta and fit reason beneath. No bottom
    # margin — the list wrapper owns the separator line and inter-posting spacing.
    return (f'<div style="margin:0;line-height:1.4;">'
            f'{_star_html(p)}{_link(p["title"], p["url"])}{_fit_pill_html(p)}'
            f'{_meta_html(p, lead)}{_fit_reason_html(p)}</div>')


def _lane_banner(title, n):
    # Prominent header separating lanes (Local vs US-Remote) in a combined report.
    return (f'<div style="color:{_C["head"]};font-family:{_FONT};font-size:21px;font-weight:800;'
            f'margin:36px 0 2px;padding-top:14px;border-top:2px solid {_C["border"]};">'
            f'{_esc(title)} <span style="color:{_C["muted"]};font-weight:600;font-size:15px;">'
            f'· {n}</span></div>')


def _html_lane(lane, show_banner):
    # HTML for one lane's sections. Appends referenced logos into _LOGOS_USED (caller clears).
    matched, new, removed, changed = lane["matched"], lane["new"], lane["removed"], lane["changed"]
    errors, first_run = lane["errors"], lane["first_run"]
    scored = any(p.get("fit_result") for p in matched)
    by_score = lambda x: -((x.get("fit_result") or {}).get("score", 0))
    B = []
    if show_banner:
        B.append(_lane_banner(lane["title"], len(matched)))

    B.append(_section("What's changed"))
    if first_run:
        B.append(_muted("First run — baseline established. Changes will appear here on the next run."))
    elif not (new or changed):   # removed/filled is rendered in its own bottom section
        B.append(_muted("No new or changed roles since the previous run."))
    else:
        if new:
            B.append(_chip(f"New · {len(new)}", "green"))
            order = sorted(new, key=by_score) if scored else sorted(new, key=lambda x: x["company"])
            for p in order:
                inner = (_star_html(p) + _link(p["title"], p["url"]) + _fit_pill_html(p)
                         + _meta_html(p, lead=p["company"]) + _fit_reason_html(p))
                B.append(_card(_icon_row(p["company"], inner), _C["green"]))
        if changed:
            B.append(_chip(f"Changed titles · {len(changed)}", "amber"))
            for o, c in changed:
                inner = (_link(c["title"], c["url"])
                         + _muted(f'{_esc(c["company"])} · was "{_esc(o["title"])}"'))
                B.append(_card(_icon_row(c["company"], inner), _C["amber"]))

    B.append(_section(f"All current matching roles ({len(matched)})"))
    if not matched:
        B.append(_muted("No roles currently match this profile."))
    else:
        order = (sorted(matched, key=by_score) if scored
                 else sorted(matched, key=lambda x: (x["company"], x["title"])))
        for p in order:
            row = _icon_row(p["company"], _role_inner(p, lead=p["company"]))
            B.append(f'<div style="border-bottom:1px solid {_C["border"]};'
                     f'padding-bottom:16px;margin-bottom:8px;">{row}</div>')

    if errors:
        B.append(_section(f"Source warnings ({len(errors)})"))
        for e in errors:
            B.append(f'<div style="color:{_C["amber"]};font-family:{_FONT};'
                     f'font-size:13px;margin:0 0 4px;">{_esc(e)}</div>')
    return "".join(B)


def _logo_tile(company, px):
    # A logo tile at an arbitrary size (the digest hero's logo row). Same source logic as
    # _logo_square — prefetched CID logo on white, else a colored monogram — returns a <td>.
    initials = _esc(_initials(company))
    slug = _company_slug(company)
    rad = max(6, px // 5)
    if slug and os.path.exists(os.path.join(LOGO_DIR, f"{slug}.png")):
        _LOGOS_USED.add(f"logos/{slug}.png")
        return (f'<td align="center" valign="middle" style="width:{px}px;height:{px}px;'
                f'background-color:#ffffff;border-radius:{rad}px;">'
                f'<img src="cid:{slug}.png" width="{px}" height="{px}" alt="{initials}" '
                f'style="display:block;border:0;border-radius:{rad}px;"></td>')
    return (f'<td align="center" valign="middle" style="width:{px}px;height:{px}px;'
            f'background-color:{_mono_color(company)};border-radius:{rad}px;color:#ffffff;'
            f'font-family:{_FONT};font-size:{max(11, px // 2 - 3)}px;font-weight:700;">{initials}</td>')


def _lane_short(title):
    # ("🌎", "US-Remote") from a lane title like "🌎 US-Remote" / "📍 Local — Silicon Slopes".
    parts = title.split(None, 1)
    emoji = parts[0] if parts else ""
    rest = re.split(r"\s+[—/]\s+", parts[1])[0].strip() if len(parts) > 1 else ""
    return emoji, rest


def _digest_counts(lanes):
    # Roll up the numbers the digest hero + preheader summarize.
    all_new = [p for l in lanes for p in l["new"]]
    return {"new": all_new,
            "changed": sum(len(l["changed"]) for l in lanes),
            "removed": sum(len(l["removed"]) for l in lanes),
            "matched": sum(len(l["matched"]) for l in lanes),
            "first_run": all(l["first_run"] for l in lanes),
            "scored": any(p.get("fit_result") for p in all_new)}


def _new_companies_by_fit(all_new, scored):
    # Distinct new-role companies, best-fit first (so the logo row leads with the strongest
    # matches), plus the fit-ordered postings (for the top pick). Falls back to alphabetical.
    order = (sorted(all_new, key=lambda p: -((p.get("fit_result") or {}).get("score", 0)))
             if scored else sorted(all_new, key=lambda p: p["company"]))
    seen = []
    for p in order:
        if p["company"] not in seen:
            seen.append(p["company"])
    return seen, order


def _summary_text(lanes):
    # Plain-text one-liner for the hidden preheader (the Gmail/inbox snippet).
    c = _digest_counts(lanes)
    n = len(c["new"])
    if n:
        parts = [f'{_lane_short(l["title"])[0]} {len(l["new"])} {_lane_short(l["title"])[1]}'
                 for l in lanes if l["new"]]
        s = f'{n} new role{"s" if n != 1 else ""} · ' + "  ".join(parts)
        if c["scored"]:
            top = max(c["new"], key=lambda p: (p.get("fit_result") or {}).get("score", -1))
            sc = (top.get("fit_result") or {}).get("score", -1)
            if sc >= 0:
                s += f' · Top: {top["title"][:44]} @ {top["company"]} ({sc})'
        return s
    if c["first_run"]:
        return f'Baseline established · {c["matched"]} roles now tracked'
    tail = []
    if c["changed"]:
        tail.append(f'{c["changed"]} title change{"s" if c["changed"] != 1 else ""}')
    if c["removed"]:
        tail.append(f'{c["removed"]} filled/removed')
    return " · ".join(tail) if tail else "No changes since the last run"


def _preheader(lanes):
    # Hidden preheader: the FIRST text in the body, so Gmail/Apple Mail use it as the inbox
    # snippet instead of scraping whatever markup comes first. Trailing invisible padding stops
    # the client from appending body text after our summary.
    pad = "&#847;&zwnj;&nbsp;" * 40
    return ('<div style="display:none !important;visibility:hidden;mso-hide:all;font-size:1px;'
            'line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;color:transparent;'
            f'height:0;width:0;">{_esc(_summary_text(lanes))}{pad}</div>')


def _digest_hero(lanes):
    # Scannable summary card at the very top of the email: headline count, per-lane new
    # breakdown, a row of logos for the newly-added companies, and the single best new pick.
    c = _digest_counts(lanes)
    all_new, scored, F = c["new"], c["scored"], _FONT
    if all_new:
        big = (f'<span style="color:{_C["green"]};font-weight:800;">{len(all_new)}</span> '
               f'new role{"s" if len(all_new) != 1 else ""} today')
    elif c["first_run"]:
        big = (f'<span style="color:{_C["link"]};font-weight:800;">{c["matched"]}</span> '
               f'role{"s" if c["matched"] != 1 else ""} now tracked')
    else:
        big = "No new roles today"
    rows = [f'<div style="color:{_C["head"]};font-family:{F};font-size:20px;font-weight:800;'
            f'line-height:1.25;">{big}</div>']

    seg = []
    for l in lanes:
        if l["new"]:
            em, short = _lane_short(l["title"])
            seg.append(f'{em}&nbsp;<b style="color:{_C["head"]};">{len(l["new"])}</b> {_esc(short)}')
    tail = []
    if c["changed"]:
        tail.append(f'{c["changed"]} changed')
    if c["removed"]:
        tail.append(f'{c["removed"]} filled/removed')
    if all_new:
        sub = " &nbsp;·&nbsp; ".join(seg)
        if tail:
            sub += f' &nbsp;·&nbsp; <span style="color:{_C["muted"]};">{" · ".join(tail)}</span>'
    elif c["first_run"]:
        sub = "Baseline set — new matches will land here from the next run."
    else:
        sub = " · ".join(tail) if tail else "No changes since the last run."
    rows.append(f'<div style="color:{_C["text"]};font-family:{F};font-size:14px;'
                f'margin-top:7px;">{sub}</div>')

    if all_new:
        companies, order = _new_companies_by_fit(all_new, scored)
        cap = 8
        cells = "".join(f'{_logo_tile(co, 34)}<td style="width:7px;"></td>' for co in companies[:cap])
        more = (f'<td valign="middle" style="color:{_C["muted"]};font-family:{F};font-size:12px;'
                f'font-weight:600;padding-left:2px;">+{len(companies) - cap}</td>'
                if len(companies) > cap else "")
        rows.append('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                    f'style="margin-top:14px;"><tr>{cells}{more}</tr></table>')
        top = order[0]
        rows.append(f'<div style="color:{_C["muted"]};font-family:{F};font-size:11px;font-weight:700;'
                    f'letter-spacing:.5px;text-transform:uppercase;margin-top:16px;">Top pick</div>')
        rows.append(f'<div style="margin-top:3px;line-height:1.4;">{_star_html(top)}'
                    f'{_link(top["title"], top["url"])}{_fit_pill_html(top)}'
                    f'<span style="color:{_C["muted"]};font-family:{F};font-size:13px;"> · '
                    f'{_esc(top["company"])}</span></div>')

    return (f'<div style="border:1px solid {_C["border"]};border-left:3px solid {_C["link"]};'
            f'background-color:{_C["panel"]};border-radius:12px;padding:18px 20px;'
            f'margin:16px 0 6px;">{"".join(rows)}</div>')


def build_html_report(profile, lanes):
    # Compose one or more lanes into a single email: a hidden preheader (inbox snippet) + a
    # digest hero (summary), then each lane, then the Removed/filled region. Clears _LOGOS_USED,
    # then the hero + lanes populate it.
    today = datetime.date.today().isoformat()
    _LOGOS_USED.clear()   # collect the logo files this report references (for CID attach)
    scored = any(p.get("fit_result") for lane in lanes for p in lane["matched"])
    B = []
    B.append(_preheader(lanes))   # hidden summary → controls the Gmail/inbox preview line
    B.append(f'<div style="color:{_C["head"]};font-family:{_FONT};font-size:22px;'
             f'font-weight:800;line-height:1.3;">{_esc(profile["label"])}</div>')
    sub = f"Job report · {today}" + (" · ranked by fit" if scored else "")
    if STAR_WITHIN_DAYS:
        sub += f" · ⭐ = posted in the last {STAR_WITHIN_DAYS} days"
    B.append(f'<div style="color:{_C["muted"]};font-family:{_FONT};font-size:14px;'
             f'margin-top:4px;">{_esc(sub)}</div>')
    B.append(_simulated_banner())
    B.append(_digest_hero(lanes))   # scannable summary card at the top of the email
    multi = len(lanes) > 1
    for lane in lanes:
        B.append(_html_lane(lane, show_banner=multi))

    # Removed / filled roles collected at the very bottom of the email, one sub-section per
    # lane (e.g. "🌎 US-Remote · 3"), so departures don't clutter each lane's "What's changed".
    removed_lanes = [lane for lane in lanes if lane["removed"]]
    if removed_lanes:
        B.append(_section("Removed / filled"))
        for lane in removed_lanes:
            B.append(_chip(f'{lane["title"]} · {len(lane["removed"])}', "red"))
            for p in sorted(lane["removed"], key=lambda x: x["company"]):
                inner = (f'<span style="color:{_C["text"]};font-family:{_FONT};'
                         f'font-weight:600;">{_esc(p["title"])}</span>'
                         + _meta_html(p, lead=p["company"]))
                B.append(_card(_icon_row(p["company"], inner), _C["red"]))

    body = "".join(B)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_esc(profile["label"])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
  body, td, div, a, span {{ font-family: {_FONT}; }}
</style>
</head>
<body style="margin:0;padding:0;background-color:{_C['bg']};font-family:{_FONT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_C['bg']};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="780" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:780px;background-color:{_C['card']};border:1px solid {_C['border']};border-radius:14px;">
<tr><td style="padding:28px 30px 32px;">{body}</td></tr>
</table>
<div style="color:{_C['muted']};font-family:{_FONT};font-size:11px;margin-top:16px;">Prospector · Silicon Slopes job monitor</div>
</td></tr>
</table>
</body>
</html>"""


# =======================================================================================
# Lisa's ranked daily digest (WS3)
# =======================================================================================
#
# A digest, not a change log: it shows the best CURRENTLY OPEN roles worth pursuing, whether
# they first appeared today or last week, because people rarely apply the day they first see
# a role. A role therefore recurs across days while it stays open and well-scored — but it
# appears exactly ONCE per email. Repetition is controlled by feedback_<name>.json, not by
# hiding things (see load_feedback).

REC_LABEL = {"apply_first": ("🔥", "Apply First", "green"),
             "strong_fit": ("⭐", "Strong Fit", "green"),
             "stretch": ("🤔", "Stretch", "amber"),
             "practical_contract": ("💵", "Practical Contract", "link")}
# Section order = recommendation priority order.
REC_RANK = {"apply_first": 0, "strong_fit": 1, "practical_contract": 2, "stretch": 3,
            "not_recommended": 9}

# Per-section quotas, not one global ranked cut. A single global cap starves whole sections:
# in testing, 18 apply_first/strong_fit roles filled the cap and the Contract section came out
# empty even though the staffing lane had matches. Quotas sum to the ~18 hard ceiling, and a
# normal day lands in the 8-12 target because most buckets aren't full.
DIGEST_QUOTAS = {"top": 6, "additional": 4, "contract": 3, "utah": 2}
DIGEST_TOTAL_CAP = 18     # hard ceiling on visible roles across all sections
DIGEST_TARGET = 12        # the usual-day target


def _is_remote(loc):
    return _matches_any((loc or "").lower(),
                        ["remote", "anywhere", "distributed", "work from home", "wfh",
                         "virtual"])


def _is_utah(loc):
    return _matches_any((loc or "").lower(), LOCAL_KEYWORDS)


def _looks_hybrid(posting):
    blob = f"{posting.get('title', '')} {posting.get('description', '')}".lower()
    return "hybrid" in blob or "days in office" in blob or "days onsite" in blob


def location_label(posting, verdict):
    """The location indicator, using only what we can actually tell.

    ✓ Remote                              — US-remote
    📍 Utah / 📍 Hybrid — City, ST         — Utah-local (Utah hybrid gets the hybrid wording)
    🏡 Hybrid — City, ST                   — hybrid outside Utah
    🏡 Relocation Required — City, ST      — onsite outside Utah, or the model saw an
                                             explicit relocation requirement
    """
    loc = (posting.get("location") or "").strip()
    reloc = bool((verdict or {}).get("relocation_required"))
    if _is_remote(loc) and not reloc:
        return "✓", "Remote", loc
    if _is_utah(loc):
        return ("📍", "Hybrid", loc) if _looks_hybrid(posting) else ("📍", "Utah", loc)
    if reloc:
        return "🏡", "Relocation Required", loc
    if _looks_hybrid(posting):
        return "🏡", "Hybrid", loc
    return "🏡", "Relocation Required", loc


# Urgency is deliberately kept SEPARATE from fit. Note the precision limit: `posted` is a
# date with no time of day (and Workday's is reverse-engineered from "Posted 3 Days Ago"),
# so the freshest band honestly means "posted today" rather than "within 24 hours".
URGENCY_BANDS = [(0, "🕐 Posted today"), (3, "🕑 Posted in the last 3 days"),
                 (7, "🕒 Posted 4–7 days ago"), (14, "🕓 Posted 8–14 days ago")]


def urgency_band(posted):
    if not posted:
        return "🕓 Posting date unknown"
    try:
        days = (datetime.date.today() - datetime.date.fromisoformat(posted)).days
    except ValueError:
        return "🕓 Posting date unknown"
    for limit, label in URGENCY_BANDS:
        if days <= limit:
            return label
    return "🕓 Posted over 14 days ago"


def _opportunities(lanes, settings, feedback):
    """Flatten every lane into one ranked list of visible opportunities, plus the bookkeeping
    the Hiring Progress section needs. Each role lands in exactly ONE section."""
    hidden = {"not_recommended": 0, "suppressed": 0, "unscored": 0}
    rows = []
    for lane in lanes:
        for p in lane["matched"]:
            v = p.get("fit_result") or {}
            fb = feedback_for(p, feedback)
            if fb and fb["status"] in SUPPRESS_STATUSES:
                hidden["suppressed"] += 1
                continue
            rec = v.get("recommendation")
            if rec == "not_recommended":
                # Retained in the snapshot for audit, never shown in an opportunity section.
                hidden["not_recommended"] += 1
                continue
            if not rec:
                # Unscored (scoring off, or the verdict failed validation). Keep it visible —
                # dropping it would silently hide roles whenever the API has a bad day.
                hidden["unscored"] += 1
                rec = "stretch"
            rows.append({"p": p, "v": v, "rec": rec, "fb": fb,
                         "lane": lane["title"], "suffix": lane.get("suffix", "")})

    def sort_key(r):
        # recommendation category, then opportunity score, then recency
        score = r["v"].get("opportunity_score", -1)
        posted = r["p"].get("posted") or ""
        return (REC_RANK.get(r["rec"], 5), -(score if score is not None else -1),
                _neg_date(posted))

    rows.sort(key=sort_key)

    # Bucket first, THEN apply per-section quotas. Precedence (first match wins, so a role
    # can never appear twice): contract lane -> Contract; strong recommendations -> Top,
    # overflowing into Additional; remaining Utah-local -> Utah; everything else -> Additional.
    pools = {"top": [], "additional": [], "contract": [], "utah": []}
    for r in rows:
        if r["suffix"] == "_staffing" or r["rec"] == "practical_contract":
            pools["contract"].append(r)
        elif r["rec"] in ("apply_first", "strong_fit"):
            pools["top"].append(r)
        elif _is_utah(r["p"].get("location")):
            pools["utah"].append(r)
        else:
            pools["additional"].append(r)

    # Strong roles beyond the Top quota drop into Additional rather than vanishing.
    overflow = pools["top"][DIGEST_QUOTAS["top"]:]
    pools["top"] = pools["top"][:DIGEST_QUOTAS["top"]]
    pools["additional"] = overflow + pools["additional"]

    sections, shown = {}, 0
    for name in ("top", "contract", "utah", "additional"):
        room = min(DIGEST_QUOTAS[name], max(0, DIGEST_TOTAL_CAP - shown))
        sections[name] = pools[name][:room]
        shown += len(sections[name])
    hidden["over_cap"] = max(0, len(rows) - shown)
    return sections, hidden


def _neg_date(posted):
    """Sort helper: newer first, unknown dates last."""
    try:
        return -datetime.date.fromisoformat(posted).toordinal()
    except (ValueError, TypeError):
        return 1


def _hiring_progress(lanes, feedback):
    """Cautious wording only. The engine cannot tell filled from pulled, so it never says
    either — and a role that merely aged out is not reported as gone."""
    applied, other, aged, filtered = [], [], 0, 0
    for lane in lanes:
        for p in lane["removed"]:
            reason = p.get("removal_reason", "not_listed")
            if reason == "aged_out":
                aged += 1
                continue          # still possibly open; not a departure
            if reason == "filter_change":
                filtered += 1
                continue          # our rules changed, not the posting
            fb = feedback_for(p, feedback)
            if fb and fb["status"] in ("applied", "already_applied"):
                applied.append(p)
            else:
                other.append(p)
    held = sum(len(lane.get("held") or []) for lane in lanes)
    return {"applied": applied, "other": other, "aged": aged, "filtered": filtered,
            "held": held}


def _score_strip(v):
    """Compact score line: the headline opportunity score plus the three dimensions."""
    opp = v.get("opportunity_score", -1)
    if opp is None or opp < 0:
        return _muted("Not scored this run — shown so it isn't silently hidden")
    def cell(label, val):
        val = "—" if val is None or val < 0 else val
        return (f'<span style="color:{_C["muted"]};font-family:{_FONT};font-size:12px;">'
                f'{label} <b style="color:{_C["text"]};">{val}</b></span>')
    return ('<div style="margin-top:6px;">'
            f'<span style="display:inline-block;background-color:{_C["green_bg"]};'
            f'color:{_C["green"]};font-family:{_FONT};font-size:13px;font-weight:800;'
            f'padding:2px 10px;border-radius:10px;">{opp}/100</span>'
            f'<span style="color:{_C["border"]};"> &nbsp;</span>'
            + " &nbsp;·&nbsp; ".join([cell("qual", v.get("qualification_fit")),
                                      cell("interest", v.get("interest_fit")),
                                      cell("practical", v.get("practical_fit"))])
            + '</div>')


def _perk_chips(v):
    """Only shown when the posting EXPLICITLY mentioned them — never inferred."""
    out = []
    if v.get("relocation_assistance_mentioned"):
        out.append(("Relocation assistance mentioned", "link"))
    if v.get("signing_bonus_mentioned"):
        out.append(("Signing bonus mentioned", "green"))
    if not out:
        return ""
    return '<div style="margin-top:5px;">' + "".join(
        f'<span style="display:inline-block;background-color:{_C[k + "_bg"]};color:{_C[k]};'
        f'font-family:{_FONT};font-size:11px;font-weight:700;padding:2px 8px;'
        f'border-radius:9px;margin-right:5px;">{_esc(t)}</span>' for t, k in out) + '</div>'


def _bullets(items, color_key, heading):
    if not items:
        return ""
    lis = "".join(
        f'<li style="margin:1px 0;">{_esc(s)}</li>' for s in items)
    return (f'<div style="color:{_C[color_key]};font-family:{_FONT};font-size:12px;'
            f'font-weight:700;margin-top:7px;">{_esc(heading)}</div>'
            f'<ul style="color:{_C["text"]};font-family:{_FONT};font-size:13px;'
            f'margin:3px 0 0;padding-left:18px;line-height:1.45;">{lis}</ul>')


def _digest_card(row):
    p, v, rec = row["p"], row["v"], row["rec"]
    emoji, label, key = REC_LABEL.get(rec, ("🤔", "Stretch", "amber"))
    lemoji, ltext, lloc = location_label(p, v)
    fb = row["fb"]

    head = (f'<div style="margin:0 0 2px;">'
            f'<span style="display:inline-block;background-color:{_C[key + "_bg"]};'
            f'color:{_C[key]};font-family:{_FONT};font-size:11px;font-weight:800;'
            f'letter-spacing:.3px;padding:3px 9px;border-radius:10px;">'
            f'{emoji} {_esc(label.upper())}</span>')
    if fb and fb["status"] == "interested":
        head += (f'<span style="display:inline-block;background-color:{_C["amber_bg"]};'
                 f'color:{_C["amber"]};font-family:{_FONT};font-size:11px;font-weight:700;'
                 f'padding:3px 9px;border-radius:10px;margin-left:6px;">★ Interested</span>')
    head += '</div>'

    title = (f'<div style="margin:3px 0 0;line-height:1.35;">'
             f'{_link(p["title"], p["url"])}</div>')

    meta_bits = [f'<b style="color:{_C["text"]};">{_esc(p["company"])}</b>',
                 f'{lemoji} {_esc(ltext)}' + (f' — {_esc(lloc)}' if lloc and ltext != "Remote"
                                              else ""),
                 _esc(urgency_band(p.get("posted")))]
    meta = _muted(" &nbsp;·&nbsp; ".join(meta_bits))
    salary = ""
    if p.get("salary"):
        salary = (f'<div style="color:{_C["green"]};font-family:{_FONT};font-size:13px;'
                  f'font-weight:700;margin-top:3px;">{_esc(p["salary"])}</div>')

    inner = (head + title + meta + salary + _score_strip(v) + _perk_chips(v)
             + _bullets(v.get("reasons") or [], "muted", "Why this appeared")
             + _bullets(v.get("concerns") or [], "amber", "Worth checking"))
    return _card(_icon_row(p["company"], inner), _C[key])


def _digest_section(heading, rows, blurb=""):
    if not rows:
        return ""
    out = [_section(f"{heading} ({len(rows)})")]
    if blurb:
        out.append(_muted(blurb))
    out += [_digest_card(r) for r in rows]
    return "".join(out)


def _digest_hero_lisa(sections, hidden, window):
    counts = {k: len(v) for k, v in sections.items()}
    total = sum(counts.values())
    F = _FONT
    rows = [f'<div style="color:{_C["head"]};font-family:{F};font-size:20px;'
            f'font-weight:800;line-height:1.25;">'
            f'<span style="color:{_C["green"]};font-weight:800;">{total}</span> '
            f'opportunit{"y" if total == 1 else "ies"} worth a look today</div>']
    seg = []
    for key, name in (("top", "top"), ("additional", "additional"),
                      ("contract", "contract"), ("utah", "Utah")):
        if counts.get(key):
            seg.append(f'<b style="color:{_C["head"]};">{counts[key]}</b> {name}')
    sub = " &nbsp;·&nbsp; ".join(seg) if seg else "No open roles clear the bar today."
    rows.append(f'<div style="color:{_C["text"]};font-family:{F};font-size:14px;'
                f'margin-top:7px;">{sub}</div>')
    quiet = []
    if hidden.get("over_cap"):
        quiet.append(f'{hidden["over_cap"]} more below the cut')
    if hidden.get("not_recommended"):
        quiet.append(f'{hidden["not_recommended"]} screened out')
    if hidden.get("suppressed"):
        quiet.append(f'{hidden["suppressed"]} hidden by your feedback')
    rows.append(f'<div style="color:{_C["muted"]};font-family:{F};font-size:12px;'
                f'margin-top:5px;">Showing open roles from the last {window} days'
                + (" &nbsp;·&nbsp; " + " · ".join(quiet) if quiet else "") + '</div>')
    # No "top pick" line here on purpose: the first card in Top Opportunities IS the top
    # pick, and repeating its link would make one job appear twice in the same email.
    return (f'<div style="border:1px solid {_C["border"]};border-left:3px solid {_C["link"]};'
            f'background-color:{_C["panel"]};border-radius:12px;padding:18px 20px;'
            f'margin:16px 0 6px;">{"".join(rows)}</div>')


def _hiring_progress_html(prog):
    """Cautious by construction: 'no longer listed' / 'no longer detected', never 'filled'."""
    if not (prog["applied"] or prog["other"] or prog["aged"] or prog["filtered"]
            or prog["held"]):
        return ""
    B = [_section("Hiring Progress")]
    for p in sorted(prog["applied"], key=lambda x: x["company"]):
        B.append(f'<div style="color:{_C["text"]};font-family:{_FONT};font-size:13px;'
                 f'margin:4px 0;"><span style="color:{_C["green"]};font-weight:700;">'
                 f'Applied ✓</span> &nbsp;{_esc(p["title"])} — {_esc(p["company"])} '
                 f'<span style="color:{_C["muted"]};">· Posting no longer listed</span></div>')
    for p in sorted(prog["other"], key=lambda x: x["company"]):
        B.append(f'<div style="color:{_C["text"]};font-family:{_FONT};font-size:13px;'
                 f'margin:4px 0;"><span style="color:{_C["muted"]};">Not applied —</span> '
                 f'{_esc(p["title"])} — {_esc(p["company"])} '
                 f'<span style="color:{_C["muted"]};">· Posting no longer detected</span>'
                 f'</div>')
    notes = []
    if prog["aged"]:
        notes.append(f'{prog["aged"]} role(s) aged past the display window and may still '
                     f'be open')
    if prog["filtered"]:
        notes.append(f'{prog["filtered"]} role(s) no longer match the current search rules')
    if prog["held"]:
        notes.append(f'{prog["held"]} role(s) held because a job source failed to respond — '
                     f'status unknown, not treated as closed')
    if notes:
        B.append(_muted("; ".join(notes) + "."))
    return "".join(B)


def build_digest_html(profile, lanes, settings, feedback=None):
    """Lisa's ranked daily digest. One appearance per job, capped visible count, sections in
    priority order, and a cautious Hiring Progress footer."""
    _LOGOS_USED.clear()
    feedback = feedback or {"by_key": {}, "by_ident": {}}
    window = profile_age_window(profile, settings) or 14
    sections, hidden = _opportunities(lanes, settings, feedback)
    prog = _hiring_progress(lanes, feedback)
    today = datetime.date.today().isoformat()

    B = [_digest_preheader(sections, hidden), _simulated_banner()]
    B.append(f'<div style="color:{_C["head"]};font-family:{_FONT};font-size:22px;'
             f'font-weight:800;line-height:1.3;">{_esc(profile["label"])}</div>')
    B.append(f'<div style="color:{_C["muted"]};font-family:{_FONT};font-size:14px;'
             f'margin-top:4px;">Daily opportunity digest · {today}</div>')
    B.append(_digest_hero_lisa(sections, hidden, window))
    B.append(_digest_section("Top Opportunities", sections["top"],
                             "Best qualified, best matched, and practical enough to act on now."))
    B.append(_digest_section("Additional Strong Opportunities", sections["additional"]))
    B.append(_digest_section("Contract Opportunities", sections["contract"],
                             "Contract or interim work — useful for income and experience."))
    B.append(_digest_section("Utah Opportunities", sections["utah"],
                             "Local roles not already listed above."))
    if not any(sections.values()):
        B.append(_muted("Nothing cleared the bar today. Removed and aged-out roles are "
                        "summarized below."))
    B.append(_hiring_progress_html(prog))
    errors = [e for lane in lanes for e in (lane.get("errors") or [])]
    if errors:
        B.append(_section(f"Source warnings ({len(errors)})"))
        for e in errors:
            B.append(f'<div style="color:{_C["amber"]};font-family:{_FONT};'
                     f'font-size:12px;margin:0 0 3px;">{_esc(e)}</div>')
    return _digest_shell(profile, "".join(B))


def _digest_preheader(sections, hidden):
    total = sum(len(v) for v in sections.values())
    top = (sections["top"] or sections["additional"] or sections["contract"]
           or sections["utah"])
    s = f'{total} opportunit{"y" if total == 1 else "ies"} today'
    if top:
        r = top[0]
        score = (r["v"] or {}).get("opportunity_score", -1)
        s += f' · Top: {r["p"]["title"][:44]} @ {r["p"]["company"]}'
        if score and score >= 0:
            s += f' ({score})'
    pad = "&#847;&zwnj;&nbsp;" * 40
    return ('<div style="display:none !important;visibility:hidden;mso-hide:all;'
            'font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;'
            'overflow:hidden;color:transparent;height:0;width:0;">'
            f'{_esc(s)}{pad}</div>')


def _digest_shell(profile, body):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_esc(profile["label"])}</title>
<style>
  body, td, div, a, span {{ font-family: {_FONT}; }}
</style>
</head>
<body style="margin:0;padding:0;background-color:{_C['bg']};font-family:{_FONT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_C['bg']};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="720" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:720px;background-color:{_C['card']};border:1px solid {_C['border']};border-radius:14px;">
<tr><td style="padding:26px 28px 30px;">{body}</td></tr>
</table>
<div style="color:{_C['muted']};font-family:{_FONT};font-size:11px;margin-top:16px;">Prospector · daily opportunity digest</div>
</td></tr>
</table>
</body>
</html>"""


def write_feedback_template(profile, lanes, feedback):
    """Write feedback_template_<name>.json next to the reports: every role currently in play,
    pre-filled with an empty status, so giving feedback is copy-paste rather than typing keys
    by hand. Never overwrites the real feedback_<name>.json."""
    seen, rows = set(), []
    for lane in lanes:
        for p in lane["matched"]:
            if p["key"] in seen:
                continue
            seen.add(p["key"])
            existing = feedback_for(p, feedback)
            rows.append({"key": p["key"], "company": p["company"], "title": p["title"],
                         "status": existing["status"] if existing else "",
                         "note": existing["note"] if existing else ""})
    rows.sort(key=lambda r: (r["company"], r["title"]))
    doc = {"_comment": ("Copy any row you want to act on into feedback_"
                        f"{profile['name']}.json under \"entries\", set \"status\" to one of: "
                        + ", ".join(FEEDBACK_STATUSES)
                        + ". This template is regenerated every run and is NOT read by "
                          "Prospector — only feedback_" + profile["name"] + ".json is."),
           "entries": rows}
    path = os.path.join(OUT_DIR, f"feedback_template_{profile['name']}.json")
    json.dump(doc, open(path, "w"), indent=1)


def _run_lane(profile, src, client, suffix, title, max_age_days=None):
    # Match + score + diff ONE lane against its own snapshot (suffix ""=local, "_remote").
    # `src` is a lane source dict: {"pool": [...], "errors": [...], "failed": {company names}}.
    # Writes the slim snapshot; returns (lane_dict, has_changes).
    pool, errors = src.get("pool") or [], src.get("errors") or []
    failed_companies = src.get("failed") or set()
    matched = [p for p in pool if matches_profile(p, profile)]
    # Per-profile display window. The shared pool is fetched at the WIDEST window any profile
    # asks for, then narrowed here — so Lisa can run a 14-day window while Chad stays at 7
    # without a second set of API calls. Applied BEFORE scoring so out-of-window roles are
    # never sent to the model.
    if max_age_days:
        matched = [p for p in matched if _within_age(p["posted"], max_age_days)]
    # Tell the scorer which lane a role came from, so a staffing-firm posting is judged as
    # contract work (and can earn `practical_contract`) rather than looking like a permanent
    # role with a suspiciously thin description.
    lane_hint = ("contract or temporary role from a staffing firm" if suffix == "_staffing"
                 else "standard direct-employer posting")
    for p in matched:
        p["_lane"] = lane_hint
    enrich_salary(matched)   # cache-deduped across profiles; postings are shared refs
    snap = os.path.join(SNAPSHOT_DIR, f"snapshot_{profile['name']}{suffix}.json")
    prev = json.load(open(snap)) if os.path.exists(snap) else None

    scored = enrich_with_fit(matched, prev, profile, client)
    if scored:
        print(f"  [{profile['name']}{suffix}] scored {scored} new role(s) with {FIT_MODEL}")
    if profile.get("fit_mode") == "filter":   # drop roles the model rated a clear "no"
        matched = [p for p in matched if (p.get("fit_result") or {}).get("fit") != "no"]

    held = []
    if prev is None:
        new, removed, changed, first_run, has_changes = [], [], [], True, True
    else:
        new, removed, changed = diff(prev, matched)
        # A source whose fetch raised contributed ZERO postings this run, so every role we
        # knew about at that company would otherwise read as "removed"/filled. Hold those
        # roles OUT of the diff and carry them forward into the snapshot below, so they
        # neither report as gone today nor come back as brand-new tomorrow.
        held    = [p for p in removed if p.get("company") in failed_companies]
        removed = [p for p in removed if p.get("company") not in failed_companies]
        first_run, has_changes = False, bool(new or removed or changed)
        if held:
            print(f"  [{profile['name']}{suffix}] held {len(held)} role(s) from "
                  f"{len(failed_companies)} failed source(s) — not reported as removed")

    # Say WHY each removed role left, so the report can use cautious, accurate wording
    # instead of calling everything "filled" (see classify_removal / REMOVAL_REASONS).
    for p in removed:
        p["removal_reason"] = classify_removal(p, profile, max_age_days)

    # `held` entries come from the previous snapshot and are already slim. `rescued` is
    # persisted (unlike the private `_rescued_for`) so a later run can tell a description
    # rescue apart from a rule change when classifying removals.
    slim = []
    for p in matched:
        rec = {k: v for k, v in p.items()
               if k != "description" and not k.startswith("_")}
        if profile.get("name") in p.get("_rescued_for", set()):
            rec["rescued"] = True
        slim.append(rec)
    slim += held
    json.dump(slim, open(snap, "w"), indent=1)
    lane = {"title": title, "matched": matched, "new": new, "removed": removed,
            "changed": changed, "errors": errors, "first_run": first_run, "held": held,
            "suffix": suffix}
    return lane, has_changes


def profile_age_window(profile, settings, suffix=""):
    """The display window for one lane of one profile, most specific first:
    the staffing lane's own override, then the profile's, then the global setting."""
    if suffix == "_staffing":
        cfg = settings.get("staffing_search") or {}
        if "max_age_days" in cfg:
            return cfg["max_age_days"]
    if "max_posting_age_days" in profile:
        return profile["max_posting_age_days"]
    return settings.get("max_posting_age_days")


def run_profile(profile, local_src, remote_src=None, staffing_src=None, client=None,
                settings=None):
    # Run the local lane and (when enabled + opted-in) the US-remote and contract/staffing
    # lanes, then compose ALL into ONE report/email per person. Each lane keeps its own
    # snapshot so diffs are independent; `changed` is the OR of the lanes; logos are the union.
    # Each `*_src` is a lane source dict (see _run_lane); None means "lane off for this person".
    # Lanes are RUN here but ordered for DISPLAY below (email order != run order).
    settings = settings or {}
    age = lambda sfx: profile_age_window(profile, settings, sfx)
    local_lane, changed = _run_lane(profile, local_src, client, "",
                                    "📍 Local — Silicon Slopes", max_age_days=age(""))
    remote_lane = staffing_lane = None
    if remote_src is not None and profile.get("remote_search"):
        remote_lane, r_changed = _run_lane(profile, remote_src, client,
                                           "_remote", "🌎 US-Remote",
                                           max_age_days=age("_remote"))
        changed = changed or r_changed
    if staffing_src is not None and profile.get("staffing_search"):
        staffing_lane, s_changed = _run_lane(profile, staffing_src, client,
                                             "_staffing", "🧑‍💼 Contract / Staffing",
                                             max_age_days=age("_staffing"))
        changed = changed or s_changed
    # Email display order: US-Remote, then Contract/Staffing, then Local Silicon Slopes.
    lanes = [lane for lane in (remote_lane, staffing_lane, local_lane) if lane is not None]

    feedback = load_feedback(profile["name"])
    report = build_report(profile, lanes)
    if profile.get("report_style") == "digest":
        # Lisa's ranked daily digest (WS3). Chad keeps the original lane-by-lane email.
        report_html = build_digest_html(profile, lanes, settings, feedback)
    else:
        report_html = build_html_report(profile, lanes)   # clears + repopulates _LOGOS_USED
    write_feedback_template(profile, lanes, feedback)
    open(os.path.join(OUT_DIR, f"report_{profile['name']}.md"), "w").write(report)
    open(os.path.join(OUT_DIR, f"report_{profile['name']}.html"), "w").write(report_html)
    logos = sorted(_LOGOS_USED)   # union of logos referenced across all lanes
    return changed, logos, report


DRYRUN_DIR = os.path.join(HERE, ".dryrun")


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="jobmonitor.py",
        description="Daily job-posting monitor. With no flags: run every enabled profile "
                    "and write production snapshots + reports.")
    p.add_argument("--profile", metavar="NAME", help="run just one profile")
    p.add_argument("--list", action="store_true", help="list profiles and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="safe test run: write snapshots + reports to .dryrun/ instead of the "
                        "repo root, seeded from a copy of the production snapshots. Never "
                        "writes production state and never writes GITHUB_OUTPUT.")
    p.add_argument("--snapshot-dir", metavar="DIR", help="read/write snapshots here")
    p.add_argument("--out-dir", metavar="DIR", help="write report_<name>.md/.html here")
    p.add_argument("--no-fit", action="store_true",
                   help="skip all LLM fit scoring (no Anthropic API calls, no cost)")
    p.add_argument("--fake-fit", action="store_true",
                   help="score locally with deterministic FAKE verdicts so the email layout can be previewed with no API key and no cost. Reports are stamped with a warning banner.")
    return p.parse_args(argv)


def _resolve_dirs(args):
    """Decide where snapshots (state) and reports (output) go.

    `--dry-run` points BOTH at .dryrun/ and re-seeds it with a fresh COPY of the production
    snapshots on every run. That gives a realistic diff *and* reuses the cached fit verdicts,
    so a dry run of already-scored roles costs no API calls — while the committed production
    snapshots are never opened for writing."""
    snap_dir = args.snapshot_dir or (DRYRUN_DIR if args.dry_run else HERE)
    out_dir  = args.out_dir      or (DRYRUN_DIR if args.dry_run else HERE)
    for d in {snap_dir, out_dir}:
        os.makedirs(d, exist_ok=True)
    if args.dry_run and not args.snapshot_dir:
        for src in glob.glob(os.path.join(HERE, "snapshot_*.json")):
            shutil.copy2(src, os.path.join(snap_dir, os.path.basename(src)))
    if snap_dir != HERE or out_dir != HERE:
        print(f"[safe run] snapshots → {snap_dir}\n[safe run] reports   → {out_dir}\n"
              f"[safe run] production snapshots and reports are untouched.\n")
    return snap_dir, out_dir


def main():
    args = _parse_args()
    profiles = json.load(open(PROFILES))["profiles"]

    if args.list:
        for p in profiles:
            state = "on " if p.get("enabled", True) else "off"
            print(f"  [{state}] {p['name']:<8} {p['label']}")
        return

    if args.profile:
        profiles = [p for p in profiles if p["name"] == args.profile]
        if not profiles:
            sys.exit(f"No profile named '{args.profile}'. Try --list.")
    else:
        profiles = [p for p in profiles if p.get("enabled", True)]

    global SNAPSHOT_DIR, OUT_DIR, STAR_WITHIN_DAYS, ALLOW_INTL_REMOTE
    SNAPSHOT_DIR, OUT_DIR = _resolve_dirs(args)
    settings = load_settings()
    max_age = settings.get("max_posting_age_days")
    STAR_WITHIN_DAYS = settings.get("star_within_days", STAR_WITHIN_DAYS)
    ALLOW_INTL_REMOTE = settings.get("allow_international_remote", ALLOW_INTL_REMOTE)
    global SIMULATED_SCORING
    if args.fake_fit and not args.no_fit:
        SIMULATED_SCORING = True
        client = SimulatedFitClient()
        print("[fake-fit] scoring locally with SIMULATED verdicts — no API calls.\n")
    else:
        client = None if args.no_fit else (
            get_client() if settings.get("fit_scoring_enabled", True) else None)

    # Fetch at the WIDEST window any enabled profile asks for; each profile then narrows to
    # its own window in _run_lane. One profile wanting 14 days must not cost a second fetch,
    # and must not widen anyone else's report.
    windows = [profile_age_window(p, settings) for p in profiles] + [max_age]
    fetch_age = 0 if any(not w for w in windows) else max(windows)
    if fetch_age != max_age:
        print(f"Fetch window widened to {fetch_age}d to cover per-profile windows "
              f"({', '.join(f'{p['name']}={profile_age_window(p, settings)}d' for p in profiles)}).")

    pool, errors, failed = collect_pool(max_age_days=fetch_age)
    local_src = {"pool": pool, "errors": errors, "failed": failed}
    age_note = f" ≤{fetch_age}d old" if fetch_age else ""
    print(f"Fetched {len(pool)} local roles{age_note} across all companies."
          f"{' Fit scoring: ON.' if client else ' Fit scoring: OFF.'}")

    # US-remote lane: fetched once (shared across profiles), gated to US-remote. Each profile
    # that opts in (remote_search:true) gets a US-Remote section merged into its ONE report.
    remote_src = None
    remote_on = bool(settings.get("remote_search", {}).get("enabled")
                     and any(p.get("remote_search") for p in profiles))
    if remote_on:
        r_pool, r_errors, r_failed = collect_pool(max_age_days=fetch_age,
                                                  config_path=REMOTE_CONFIG, gate=is_us_remote)
        remote_src = {"pool": r_pool, "errors": r_errors, "failed": r_failed}
        print(f"Fetched {len(r_pool)} US-remote role(s) across the remote registry.")

    # Contract/staffing lane: staffing-firm feeds (e.g. Aquent), fetched once and gated with
    # the local gate (is_local keeps Utah-local + US-remote roles, dropping other-metro on-site
    # and international remote). Each profile that opts in (staffing_search:true) gets a
    # Contract/Staffing section merged into its ONE report. Contract-only filtering happens in
    # the fetcher (placement_type), so this pool is already scoped to contract roles.
    staffing_src = None
    staffing_cfg = settings.get("staffing_search", {})
    staffing_on = bool(staffing_cfg.get("enabled")
                       and any(p.get("staffing_search") for p in profiles))
    if staffing_on:
        # The contract lane can look back further than the leadership lanes (income-focused
        # search wants volume): staffing_search.max_age_days overrides the global window,
        # falling back to it when unset.
        staffing_age = max([profile_age_window(p, settings, "_staffing") or 0
                            for p in profiles] or [0]) or None
        s_pool, s_errors, s_failed = collect_pool(max_age_days=staffing_age,
                                                  config_path=STAFFING_CONFIG, gate=is_local)
        staffing_src = {"pool": s_pool, "errors": s_errors, "failed": s_failed}
        s_note = f" ≤{staffing_age}d old" if staffing_age else ""
        print(f"Fetched {len(s_pool)} contract/staffing role(s){s_note} across the staffing registry.")
    print()

    changed_profiles = []
    logos_by_profile = {}
    for p in profiles:
        rs = remote_src if p.get("remote_search") else None
        ss = staffing_src if p.get("staffing_search") else None
        changed, logos, report = run_profile(p, local_src, remote_src=rs,
                                             staffing_src=ss, client=client,
                                             settings=settings)
        if changed:
            changed_profiles.append(p["name"])
        logos_by_profile[p["name"]] = logos
        print("=" * 70)
        print(f"[{p['name']}] changes since last run: {'yes' if changed else 'no'}"
              f" · {len(logos)} logo(s) to embed")
        print(report)

    # Expose per-profile outputs to GitHub Actions: a "changed" flag so the workflow emails
    # only people whose (combined local+remote) report changed, and the exact list of logo
    # files to attach inline as CID images — only those the report references, so none show
    # as stray downloads. One report/email per person, so no separate remote-lane outputs.
    summary = fit_usage_summary()
    if summary:
        print(summary)

    # A dry run must never signal the workflow to send email, even if GITHUB_OUTPUT is set.
    gh_out = None if args.dry_run else os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            for p in profiles:
                name = p["name"]
                fh.write(f"{name}_changed="
                         f"{'true' if name in changed_profiles else 'false'}\n")
                fh.write(f"{name}_logos={','.join(logos_by_profile.get(name, []))}\n")


if __name__ == "__main__":
    main()
