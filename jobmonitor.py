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

import hashlib, json, os, re, sys, html, urllib.request, datetime

HERE     = os.path.dirname(os.path.abspath(__file__))
CONFIG   = os.path.join(HERE, "companies.json")
REMOTE_CONFIG = os.path.join(HERE, "remote_companies.json")  # US-remote lane registry
STAFFING_CONFIG = os.path.join(HERE, "staffing_companies.json")  # contract/staffing lane registry
PROFILES = os.path.join(HERE, "profiles.json")
SETTINGS = os.path.join(HERE, "settings.json")

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
FIT_MODEL = "claude-sonnet-5"   # judgment task; swap to "claude-haiku-4-5-20251001" for lower cost
DESC_LIMIT = 2000               # chars of job description sent to the model

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


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday,
            "ashby": fetch_ashby, "recruitee": fetch_recruitee, "personio": fetch_personio,
            "aquent": fetch_aquent, "snaphop": fetch_snaphop}


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


def matches_profile(posting, profile):
    title = posting["title"].lower()
    if _any_term(title, profile.get("exclude_any", [])):
        return False
    for group in profile.get("match_groups", []):
        if not _any_term(title, group):
            return False
    return True


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
    # Short hash of the candidate content actually sent to the model. Changing the
    # background file changes this, which invalidates cached verdicts (see enrich_with_fit).
    return hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()[:12]


def score_fit(candidate, posting, client):
    """Ask the model whether the candidate is a plausible fit. Returns
    {'fit': yes|maybe|no, 'score': 0-100, 'reason': str}. Never raises — on any
    failure returns a neutral 'maybe' with score -1 so the role is kept, not dropped."""
    desc = posting.get("description") or "(no description available — judge from title and location)"
    prompt = (
        "You screen job postings for one specific candidate. Decide whether this role is "
        "worth the candidate's attention. Be realistic: reward strong matches on seniority, "
        "function, and domain; penalize clear mismatches. Many strong-fit roles are poorly or "
        "generically titled, so weight the actual mandate, scope, and problems described in the "
        "posting over title keywords.\n\n"
        f"CANDIDATE:\n{json.dumps(candidate, indent=2)}\n\n"
        f"JOB POSTING:\nTitle: {posting['title']}\nCompany: {posting['company']}\n"
        f"Location: {posting['location']}\nDescription: {desc[:DESC_LIMIT]}\n\n"
        'Respond with ONLY a JSON object, no prose and no markdown fences:\n'
        '{"fit": "yes" | "maybe" | "no", "score": <integer 0-100>, "reason": "<20 words max>"}'
    )
    try:
        # max_tokens generous so a verbose reason can't truncate the JSON mid-string.
        msg = client.messages.create(model=FIT_MODEL, max_tokens=400,
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        # Pull the JSON object out even if the model wrapped it in fences or prose.
        m = re.search(r"\{.*\}", text, re.S)
        r = json.loads(m.group(0) if m else text)
        fit = str(r.get("fit", "maybe")).lower()
        return {"fit": fit if fit in ("yes", "maybe", "no") else "maybe",
                "score": int(r.get("score", 50)),
                "reason": str(r.get("reason", ""))[:200]}
    except Exception as e:
        # Log the raw reply so CI shows what didn't parse; role is kept (score -1).
        raw = locals().get("text", "")
        print(f"[warn] fit parse failed [{posting.get('key', '?')}]: "
              f"{type(e).__name__}: {str(e)[:80]} | raw={raw[:120]!r}")
        return {"fit": "maybe", "score": -1, "reason": f"(scoring unavailable: {type(e).__name__})"}


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
    calls this with config_path=REMOTE_CONFIG and gate=is_us_remote."""
    if not os.path.exists(config_path):
        return [], []
    cfg = json.load(open(config_path))
    keep = gate or (is_local if LOCAL_ONLY else None)
    pool, errors = [], []
    for c in cfg.get("companies", []):
        try:
            got = FETCHERS[c["ats"]](c)
            if keep:
                got = [p for p in got if keep(p["location"])]
            got = [p for p in got if _within_age(p["posted"], max_age_days)]
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


def build_html_report(profile, lanes):
    # Compose one or more lanes into a single email. Single lane (remote off) is byte-for-byte
    # the old layout; two lanes get a banner each. Clears _LOGOS_USED, then both lanes populate it.
    today = datetime.date.today().isoformat()
    _LOGOS_USED.clear()   # collect the logo files this report references (for CID attach)
    scored = any(p.get("fit_result") for lane in lanes for p in lane["matched"])
    B = []
    B.append(f'<div style="color:{_C["head"]};font-family:{_FONT};font-size:22px;'
             f'font-weight:800;line-height:1.3;">{_esc(profile["label"])}</div>')
    sub = f"Job report · {today}" + (" · ranked by fit" if scored else "")
    if STAR_WITHIN_DAYS:
        sub += f" · ⭐ = posted in the last {STAR_WITHIN_DAYS} days"
    B.append(f'<div style="color:{_C["muted"]};font-family:{_FONT};font-size:14px;'
             f'margin-top:4px;">{_esc(sub)}</div>')
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


def _run_lane(profile, pool, errors, client, suffix, title):
    # Match + score + diff ONE lane against its own snapshot (suffix ""=local, "_remote").
    # Writes the slim snapshot; returns (lane_dict, has_changes).
    matched = [p for p in pool if matches_profile(p, profile)]
    enrich_salary(matched)   # cache-deduped across profiles; postings are shared refs
    snap = os.path.join(HERE, f"snapshot_{profile['name']}{suffix}.json")
    prev = json.load(open(snap)) if os.path.exists(snap) else None

    scored = enrich_with_fit(matched, prev, profile, client)
    if scored:
        print(f"  [{profile['name']}{suffix}] scored {scored} new role(s) with {FIT_MODEL}")
    if profile.get("fit_mode") == "filter":   # drop roles the model rated a clear "no"
        matched = [p for p in matched if (p.get("fit_result") or {}).get("fit") != "no"]

    if prev is None:
        new, removed, changed, first_run, has_changes = [], [], [], True, True
    else:
        new, removed, changed = diff(prev, matched)
        first_run, has_changes = False, bool(new or removed or changed)

    slim = [{k: v for k, v in p.items() if k != "description" and not k.startswith("_")}
            for p in matched]
    json.dump(slim, open(snap, "w"), indent=1)
    lane = {"title": title, "matched": matched, "new": new, "removed": removed,
            "changed": changed, "errors": errors, "first_run": first_run}
    return lane, has_changes


def run_profile(profile, local_pool, remote_pool, errors, client=None, remote_errors=None,
                staffing_pool=None, staffing_errors=None):
    # Run the local lane and (when enabled + opted-in) the US-remote and contract/staffing
    # lanes, then compose ALL into ONE report/email per person. Each lane keeps its own
    # snapshot so diffs are independent; `changed` is the OR of the lanes; logos are the union.
    # Lanes are RUN here but ordered for DISPLAY below (email order != run order).
    local_lane, changed = _run_lane(profile, local_pool, errors, client, "",
                                    "📍 Local — Silicon Slopes")
    remote_lane = staffing_lane = None
    if remote_pool is not None and profile.get("remote_search"):
        remote_lane, r_changed = _run_lane(profile, remote_pool, remote_errors or [], client,
                                           "_remote", "🌎 US-Remote")
        changed = changed or r_changed
    if staffing_pool is not None and profile.get("staffing_search"):
        staffing_lane, s_changed = _run_lane(profile, staffing_pool, staffing_errors or [], client,
                                             "_staffing", "🧑‍💼 Contract / Staffing")
        changed = changed or s_changed
    # Email display order: US-Remote, then Contract/Staffing, then Local Silicon Slopes.
    lanes = [lane for lane in (remote_lane, staffing_lane, local_lane) if lane is not None]

    report = build_report(profile, lanes)
    report_html = build_html_report(profile, lanes)   # clears + repopulates _LOGOS_USED
    open(os.path.join(HERE, f"report_{profile['name']}.md"), "w").write(report)
    open(os.path.join(HERE, f"report_{profile['name']}.html"), "w").write(report_html)
    logos = sorted(_LOGOS_USED)   # union of logos referenced across all lanes
    return changed, logos, report


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

    global STAR_WITHIN_DAYS, ALLOW_INTL_REMOTE
    settings = load_settings()
    max_age = settings.get("max_posting_age_days")
    STAR_WITHIN_DAYS = settings.get("star_within_days", STAR_WITHIN_DAYS)
    ALLOW_INTL_REMOTE = settings.get("allow_international_remote", ALLOW_INTL_REMOTE)
    client = get_client() if settings.get("fit_scoring_enabled", True) else None

    pool, errors = collect_pool(max_age_days=max_age)
    age_note = f" ≤{max_age}d old" if max_age else ""
    print(f"Fetched {len(pool)} local roles{age_note} across all companies."
          f"{' Fit scoring: ON.' if client else ' Fit scoring: OFF.'}")

    # US-remote lane: fetched once (shared across profiles), gated to US-remote. Each profile
    # that opts in (remote_search:true) gets a US-Remote section merged into its ONE report.
    remote_pool, remote_errors = [], []
    remote_on = bool(settings.get("remote_search", {}).get("enabled")
                     and any(p.get("remote_search") for p in profiles))
    if remote_on:
        remote_pool, remote_errors = collect_pool(max_age_days=max_age,
                                                  config_path=REMOTE_CONFIG, gate=is_us_remote)
        print(f"Fetched {len(remote_pool)} US-remote role(s) across the remote registry.")

    # Contract/staffing lane: staffing-firm feeds (e.g. Aquent), fetched once and gated with
    # the local gate (is_local keeps Utah-local + US-remote roles, dropping other-metro on-site
    # and international remote). Each profile that opts in (staffing_search:true) gets a
    # Contract/Staffing section merged into its ONE report. Contract-only filtering happens in
    # the fetcher (placement_type), so this pool is already scoped to contract roles.
    staffing_pool, staffing_errors = [], []
    staffing_cfg = settings.get("staffing_search", {})
    staffing_on = bool(staffing_cfg.get("enabled")
                       and any(p.get("staffing_search") for p in profiles))
    if staffing_on:
        # The contract lane can look back further than the leadership lanes (income-focused
        # search wants volume): staffing_search.max_age_days overrides the global window,
        # falling back to it when unset.
        staffing_age = staffing_cfg.get("max_age_days", max_age)
        staffing_pool, staffing_errors = collect_pool(max_age_days=staffing_age,
                                                      config_path=STAFFING_CONFIG, gate=is_local)
        s_note = f" ≤{staffing_age}d old" if staffing_age else ""
        print(f"Fetched {len(staffing_pool)} contract/staffing role(s){s_note} across the staffing registry.")
    print()

    changed_profiles = []
    logos_by_profile = {}
    for p in profiles:
        rp = remote_pool if (remote_on and p.get("remote_search")) else None
        sp = staffing_pool if (staffing_on and p.get("staffing_search")) else None
        changed, logos, report = run_profile(p, pool, rp, errors, client,
                                             remote_errors=remote_errors,
                                             staffing_pool=sp, staffing_errors=staffing_errors)
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
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            for p in profiles:
                name = p["name"]
                fh.write(f"{name}_changed="
                         f"{'true' if name in changed_profiles else 'false'}\n")
                fh.write(f"{name}_logos={','.join(logos_by_profile.get(name, []))}\n")


if __name__ == "__main__":
    main()
