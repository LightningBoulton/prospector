#!/usr/bin/env python3
"""
lifecycle.py — Prospector's persistent job discovery database.

WHY THIS EXISTS
---------------
Before this module, Prospector's only memory was `snapshot_<name><lane>.json`: a slim list
of the roles matching *right now*. That is enough to compute a diff and nothing else, and it
caused three of the reported problems directly:

  * A role could enter and leave the snapshot without ever being rendered in an email (the
    digest caps visible cards well below the size of the active set), and then show up under
    "removed" — a job the reader had never been told about. Nothing recorded whether a role
    had ever actually been SHOWN.
  * Nothing distinguished "first seen" from "posted", so age rules could not be applied
    honestly to roles whose posting date the source never gave us.
  * The same job arriving from two feeds was two unrelated rows.

This module owns the durable record. One JSON file per profile (`jobs_<name>.json`), one
record per REAL job — not per feed appearance — carrying its whole life: where it was found,
when it was first seen, when it was last verified as still listed, what the model thought of
it, what the human decided about it, and if it left, when and why.

DESIGN CONSTRAINTS (inherited from the project, and deliberate)
  * Standard library only.
  * The file is state and is committed by CI, so it must stay readable and diff-friendly.
  * Nothing here makes a network call except `verify_open`, which the caller must budget.
"""

import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DB_VERSION = 1

# ---- lifecycle states -------------------------------------------------------------------
# A record is in exactly one of these. `new`/`active`/`aging` are all "in the active
# inventory"; `stale`/`closed`/`removed` are not.
STATUS_NEW = "new"          # first seen on this run
STATUS_ACTIVE = "active"    # still listed, inside the priority window
STATUS_AGING = "aging"      # still listed, 8-14 days old — worth applying, not new
STATUS_STALE = "stale"      # over the age limit and not exceptional; stops being repeated
STATUS_CLOSED = "closed"    # independently verified gone (404/410 at the posting URL)
STATUS_REMOVED = "removed"  # legacy: written by pre-verification runs, never assigned now

ACTIVE_STATUSES = (STATUS_NEW, STATUS_ACTIVE, STATUS_AGING)
GONE_STATUSES = (STATUS_CLOSED, STATUS_REMOVED)

# ---- verification state — SEPARATE from the lifecycle status above ------------------------
#
# The lifecycle status answers "how old and how repeatable is this?". Verification answers a
# different question that was previously conflated with it: "do we actually KNOW this role is
# still open, and who told us?"
#
# Conflating them produced the reported bug. A role vanishing from a feed used to be written
# straight to STATUS_REMOVED and reported as gone, when all that had really happened was that
# an aggregator stopped returning it, or a source hiccuped. Three states, and only one of them
# is allowed to say a role is closed:
VERIFIED_LIVE = "live_verified"       # an authoritative source listed it on THIS run
NEEDS_RECHECK = "needs_recheck"       # it was live before and we cannot see it right now
VERIFIED_CLOSED = "closed_verified"   # the employer/ATS says so: 404/410, "no longer
                                      # available", or the posting's own deadline has passed
VERIFICATION_STATES = (VERIFIED_LIVE, NEEDS_RECHECK, VERIFIED_CLOSED)

VERIFICATION_LABEL = {
    VERIFIED_LIVE: "Live — verified at the source",
    NEEDS_RECHECK: "Needs recheck — not visible right now",
    VERIFIED_CLOSED: "Closed — confirmed at the employer",
}

# Why a record is in NEEDS_RECHECK. Kept explicit so the email can say which happened rather
# than implying the role went away.
RECHECK_REASONS = {
    "source_failed": "the source that carried it failed this run",
    "not_returned": "its source stopped returning it",
    "aggregator_only": "only an aggregator listed it this run",
}

# ---- source authority (requirement 5) -----------------------------------------------------
# direct employer / ATS  >  reputable secondary (a staffing firm that is itself the applier)
#  >  aggregator / feed. When sources disagree, the employer wins — so an aggregator dropping
# a role proves nothing, while the employer's own board dropping it is worth checking.
AUTHORITY_EMPLOYER = 0
AUTHORITY_SECONDARY = 1
AUTHORITY_AGGREGATOR = 2
AUTHORITATIVE = (AUTHORITY_EMPLOYER, AUTHORITY_SECONDARY)

# ---- per-run change state (requirement 6) --------------------------------------------------
# What happened to this record on THIS run, as distinct from what it is. "New" is the one that
# was being over-claimed: a role that came back after a feed blip, or arrived from a second
# source, or had its punctuation changed, used to read as a brand-new discovery.
CHANGE_NEW = "new"                # genuinely never recorded before
CHANGE_REOPENED = "reopened"      # we had confirmed it CLOSED, and it is listed again
CHANGE_REVALIDATED = "revalidated"  # it was missing / needing recheck, and it is back
CHANGE_UPDATED = "updated"        # continuously open, but something material changed
CHANGE_STILL_OPEN = "still_open"  # continuously open, unchanged
CHANGE_STATES = (CHANGE_NEW, CHANGE_REOPENED, CHANGE_REVALIDATED, CHANGE_UPDATED,
                 CHANGE_STILL_OPEN)

CHANGE_LABEL = {
    CHANGE_NEW: "NEW",
    CHANGE_REOPENED: "REOPENED",
    CHANGE_REVALIDATED: "REVALIDATED",
    CHANGE_UPDATED: "UPDATED",
    CHANGE_STILL_OPEN: "STILL OPEN",
}

# ---- age bands (requirement D) -----------------------------------------------------------
BAND_NEW = "new"            # 0-7 days   — priority discovery
BAND_APPLY = "apply"        # 8-14 days  — still worth applying if the fit holds
BAND_OLD = "old"            # 15+ days   — do not repeat unless exceptional AND verified open
NEW_BAND_DAYS = 7
APPLY_BAND_DAYS = 14

# Removal reasons we can state honestly. The engine cannot tell "filled" from "pulled", so
# it never claims either — it reports what it observed.
REMOVAL_REASONS = {
    "verified_closed": "Verified closed at the source (404 / posting removed)",
    "not_listed": "No longer listed by its source",
    "aged_out": "Aged past the display window (may still be open)",
    "filter_change": "No longer matches the current search rules",
    "expired": "Passed the expiry date the posting itself stated",
    "source_failed": "Held: the source that carried it failed this run",
    "not_returned": "Not returned by its source — needs a recheck, not yet closed",
}

# ---- human decisions (requirement H) ------------------------------------------------------
# Explicit, enumerated, auditable. No inference, no learned weights — a decision does exactly
# what it says and nothing else.
DECISIONS = ("pursue", "applied", "not_interested")
REJECTION_REASONS = ("wrong_function", "too_technical", "compensation", "location",
                     "seniority", "industry_requirement", "weak_fit", "duplicate", "closed")


def _today():
    return datetime.date.today().isoformat()


def _days_since(iso, today=None):
    """Whole days between an ISO date and `today`; None when the date is unusable."""
    if not iso:
        return None
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return None
    ref = datetime.date.fromisoformat(today) if today else datetime.date.today()
    return (ref - d).days


# ---- identity ----------------------------------------------------------------------------

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")
# Company suffixes and title decorations that differ between feeds for the same job.
_COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|holdings|group|"
                            r"technologies|technology|labs|software)\b")
_TITLE_NOISE = re.compile(r"\b(remote|hybrid|onsite|on site|full time|part time|contract|"
                          r"contractor|temporary|temp|permanent|us|usa|united states)\b")


def _norm_text(s):
    s = _PUNCT.sub(" ", (s or "").lower())
    return _SPACE.sub(" ", s).strip()


def norm_company(name):
    return _SPACE.sub(" ", _COMPANY_NOISE.sub(" ", _norm_text(name))).strip()


def norm_title(title):
    """Normalize a title for cross-feed comparison. Seniority and function words are LEFT
    ALONE — only decorations that vary between boards are stripped, so "Director,
    Operations" and "Senior Director, Operations" stay different jobs."""
    t = _norm_text(title)
    t = re.sub(r"\(.*?\)", " ", t)          # "(Remote)", "(US)", "(Contract)"
    t = _TITLE_NOISE.sub(" ", t)
    return _SPACE.sub(" ", t).strip()


def location_bucket(location):
    """A coarse location key: remote roles collapse to "remote", everything else to its first
    place token. Fine enough that the same job on two boards matches, coarse enough that two
    genuinely different city openings do not."""
    l = (location or "").lower()
    if re.search(r"\b(remote|anywhere|distributed|work from home|wfh|virtual)\b", l):
        return "remote"
    first = _norm_text(l.split(",")[0].split("(")[0])
    return first or "unknown"


# Query parameters that identify the CLICK, not the job. Stripping them is what stops the
# same requisition arriving with a new tracking tag from reading as a different job.
_TRACKING_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
                    "src", "source", "ref", "referrer", "gh_src", "lever-origin",
                    "lever-source", "trackingid", "trk", "utm_id", "rx_campaign",
                    "rx_source", "rx_medium", "sponsored", "from")


def canonical_url_key(url):
    """A stable identity for a posting URL: lowercased host and path, tracking parameters
    dropped, fragment dropped, trailing slash dropped.

    Requirement 6 names this as one of the three identities a role is persisted under, and it
    is the one that catches the common recycling case — the same employer ATS link re-shared
    with a different `utm_source`, which is otherwise indistinguishable from a fresh posting."""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
    except Exception:                                   # noqa: BLE001
        return str(url).strip().lower()
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in _TRACKING_PARAMS]
    query.sort()
    path = (parts.path or "").rstrip("/")
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return urllib.parse.urlunsplit(("", host, path.lower(),
                                    urllib.parse.urlencode(query), "")).lstrip("/")


def requisition_id(posting):
    """The employer's own requisition id, when the feed gave us one.

    `key` is built as "<Company>::<ats_id>" (see jobmonitor._norm), so the half after the
    separator IS the ATS's identifier for the requisition. It is the strongest identity we
    have: it survives a re-title, a location edit, and a move between feeds."""
    key = posting.get("key") or ""
    ident = key.split("::", 1)[1] if "::" in key else ""
    return f"{norm_company(posting.get('company'))}::{ident.strip().lower()}" if ident else ""


def identity_aliases(posting):
    """Every identity a job may be recognised by, strongest first (requirement 6).

    Order is the resolution order in `resolve_key`: requisition id, then canonical employer
    URL, then company+title+location. A match on ANY of them is the same job."""
    out = []
    req = requisition_id(posting)
    if req:
        out.append("req:" + req)
    url = canonical_url_key(posting.get("url"))
    if url:
        out.append("url:" + url)
    if posting.get("key"):
        out.append("src:" + str(posting["key"]))
    return out


def resolve_key(db, posting):
    """Find the job_key of the record this posting belongs to, or None.

    This is the whole of requirement 6's "a previously seen role must not become NEW merely
    because another source found it / a tracking parameter changed / the title wording moved".
    The composite company+title+location key catches most of it; the alias index catches the
    rest, because a requisition id and a canonical URL are both stable across exactly the
    changes that break a title comparison."""
    key = dedupe_key(posting)
    if key in db.get("jobs", {}):
        return key
    aliases = db.get("aliases") or {}
    for alias in identity_aliases(posting):
        found = aliases.get(alias)
        if found and found in db.get("jobs", {}):
            return found
    return None


def register_aliases(db, rec, posting):
    """Point every identity this posting carries at the record that owns it."""
    aliases = db.setdefault("aliases", {})
    job_key = rec["job_key"]
    for alias in identity_aliases(posting) + ["key:" + dedupe_key(posting)]:
        aliases[alias] = job_key


def dedupe_key(posting):
    """The DURABLE identity of a job, independent of which feed carried it.

    This is what makes one record out of the same role appearing on the employer's Greenhouse
    board and on Himalayas. It is deliberately NOT the ATS key — that changes per source —
    and deliberately includes a coarse location, so two real openings in two cities do not
    silently merge into one."""
    return "|".join([norm_company(posting.get("company")),
                     norm_title(posting.get("title")),
                     location_bucket(posting.get("location"))])


# ---- canonical URL ------------------------------------------------------------------------
# Requirement C: prefer the employer or ATS URL. Aggregators redirect, expire, and sometimes
# point at their own landing page rather than the application form.
_AGGREGATOR_ATS = {"himalayas", "wwr", "jobicy"}
_EMPLOYER_ATS = {"greenhouse", "lever", "smartrecruiters", "workday", "ashby", "recruitee",
                 "personio", "phenom"}


def url_rank(ats):
    """Lower is better."""
    if ats in _EMPLOYER_ATS:
        return 0
    if ats in _AGGREGATOR_ATS:
        return 2
    return 1        # staffing firms (aquent, snaphop, roberthalf): the firm IS the applier


# ---- work arrangement ---------------------------------------------------------------------

def work_arrangement(posting):
    """remote / hybrid / onsite, from what the posting actually says. 'unknown' is a real
    answer and is preferred to a guess."""
    loc = (posting.get("location") or "").lower()
    blob = f"{posting.get('title', '')} {posting.get('description', '')}".lower()
    if re.search(r"\bhybrid\b", loc) or re.search(r"\bhybrid\b|days in office|days onsite", blob):
        return "hybrid"
    if re.search(r"\b(remote|anywhere|distributed|work from home|wfh|virtual)\b", loc):
        return "remote"
    if loc.strip():
        return "onsite"
    return "unknown"


# ---- priority ------------------------------------------------------------------------------
# Priority is a FUNCTION of the model's recommendation and the posting's age — not a separate
# judgment. Keeping it derived means it can never drift out of step with the verdict, and any
# row in the Google Sheet can be explained from the two columns next to it.
PRIORITY_P1 = "P1"      # act now
PRIORITY_P2 = "P2"      # worth reviewing
PRIORITY_P3 = "P3"      # background / wildcard


def priority_for(recommendation, band, confidence="medium"):
    if recommendation == "apply_first" and band in (BAND_NEW, BAND_APPLY):
        return PRIORITY_P1
    if recommendation in ("apply_first", "strong_fit"):
        return PRIORITY_P2 if confidence != "low" else PRIORITY_P3
    if recommendation == "practical_contract":
        return PRIORITY_P2 if band == BAND_NEW else PRIORITY_P3
    return PRIORITY_P3


def band_for(record, today=None):
    """Age band from the POSTING date, falling back to first_seen when the source never gave
    one. Tracking the two separately (requirement D) is what makes that fallback honest:
    an undated posting is aged from when WE first saw it, and the record says so."""
    days = _days_since(record.get("posted_date"), today)
    if days is None:
        days = _days_since(record.get("first_seen"), today)
    if days is None:
        return BAND_NEW
    if days <= NEW_BAND_DAYS:
        return BAND_NEW
    if days <= APPLY_BAND_DAYS:
        return BAND_APPLY
    return BAND_OLD


def age_days(record, today=None):
    d = _days_since(record.get("posted_date"), today)
    if d is None:
        d = _days_since(record.get("first_seen"), today)
    return d


# ---- the database --------------------------------------------------------------------------

def db_path(profile_name, directory):
    return os.path.join(directory, f"jobs_{profile_name}.json")


def load_db(profile_name, directory):
    """Load the durable record set. A missing or corrupt file yields an empty DB rather than
    an exception — losing history is bad, but failing the daily run is worse, and the file is
    rebuilt from the next run onward."""
    path = db_path(profile_name, directory)
    if not os.path.exists(path):
        return {"version": DB_VERSION, "jobs": {}, "aliases": {}}
    try:
        raw = json.load(open(path))
    except Exception as e:                       # noqa: BLE001
        print(f"[warn] jobs_{profile_name}.json unreadable ({type(e).__name__}); "
              f"starting a fresh discovery database.")
        return {"version": DB_VERSION, "jobs": {}, "aliases": {}}
    jobs = raw.get("jobs")
    if not isinstance(jobs, dict):
        return {"version": DB_VERSION, "jobs": {}, "aliases": {}}
    aliases = raw.get("aliases")
    db = {"version": raw.get("version", DB_VERSION), "jobs": jobs,
          "aliases": aliases if isinstance(aliases, dict) else {}}
    if not db["aliases"]:
        # A database written before requisition-id / URL identity existed has no alias index.
        # Rebuild what we can from the source keys already on each record, so the first run
        # after the upgrade recognises its own history instead of re-reporting all of it as
        # new. Records only gain their URL and requisition aliases as they are seen again.
        for job_key, rec in jobs.items():
            for src_key in rec.get("source_keys") or []:
                db["aliases"]["src:" + str(src_key)] = job_key
            url = canonical_url_key(rec.get("canonical_url"))
            if url:
                db["aliases"]["url:" + url] = job_key
    return db


def save_db(db, profile_name, directory):
    path = db_path(profile_name, directory)
    doc = {"_comment": (
        "Prospector's persistent job discovery database — one record per real job, keyed by "
        "company+title+location so the same role found on several feeds is ONE row. This is "
        "state: it carries first_seen, ever_shown and the human decision fields, and the "
        "daily digest's 'removed' section is derived from it. Do not delete casually."),
        "version": DB_VERSION, "generated": _today(),
        "decisions": list(DECISIONS), "rejection_reasons": list(REJECTION_REASONS),
        # The alias index (requisition id / canonical URL / source key -> job_key). It is
        # what stops a role that changed feeds, tracking tag or title wording from being
        # re-reported as new, so it is state and is written with the records.
        "aliases": db.get("aliases", {}),
        "jobs": db["jobs"]}
    json.dump(doc, open(path, "w"), indent=1, sort_keys=False)


def new_record(posting, lane, today):
    """A fresh record with every field requirement C asks for, so a row is never partially
    shaped and downstream code never has to guess whether a key exists."""
    return {
        "job_key": dedupe_key(posting),
        "company": posting.get("company", ""),
        "title": posting.get("title", ""),
        "canonical_url": posting.get("url", ""),
        "canonical_ats": posting.get("_ats") or "",
        "sources": [],                  # every registry row that has carried this job
        "source_keys": [],              # every per-ATS key, so diffs can map back
        "posted_date": posting.get("posted") or "",
        "first_seen": today,
        "last_seen": today,
        "last_verified": today,
        "location": posting.get("location", ""),
        "work_arrangement": work_arrangement(posting),
        "employment_type": posting.get("employment_type") or "",
        "compensation": posting.get("salary") or "",
        "expires": posting.get("expires") or "",
        "fit_score": None,
        "recommendation": "",
        "confidence": "",
        "priority": PRIORITY_P3,
        "why_fits": "",
        "top_concern": "",
        "tier": posting.get("tier") or "",
        "unmet_requirements": [],
        "bridge_skills": [],
        "recruiting_gaps": [],
        "remote_scope": "",
        "remote_verified": False,
        "downgraded_from": "",
        # Set by the retrieval-time technical screen: the role was kept because it carries a
        # business mandate, but its description also talks about hands-on engineering, so it
        # can never be a top recommendation. See jobmonitor.enforce_hard_requirements.
        "technical_risk": bool(posting.get("technical_risk")),
        # An employer the reader singled out (registry `priority: true`). Deliberately NOT
        # called `priority` — that key below is the P1/P2/P3 urgency band.
        "priority_employer": bool(posting.get("priority_employer")),
        "priority_note": posting.get("priority_note") or "",
        "demoted": bool(posting.get("demoted")),
        "match_reason": posting.get("match_reason") or "",
        "lane": lane,
        "status": STATUS_NEW,
        # Verification is deliberately separate from `status` — see the constants above.
        # A brand-new record was, by definition, just listed by a source; whether that source
        # was authoritative is settled in refresh_statuses.
        "verification": VERIFIED_LIVE,
        "verification_reason": "",
        "missing_since": "",
        "change_state": CHANGE_NEW,
        "requisition_id": requisition_id(posting),
        "ever_shown": False,
        "first_shown": "",
        "last_shown": "",
        "times_shown": 0,
        "user_decision": "",
        "rejection_reason": "",
        "decision_date": "",
        "closed_date": "",
        "removal_reason": "",
    }


MATERIAL_FIELDS = ("title", "location", "compensation", "canonical_url", "employment_type")


def _merge_posting(rec, posting, lane, today):
    """Fold one feed appearance into an existing record."""
    rec["last_seen"] = today
    rec["last_verified"] = today
    # Best source authority seen on THIS run, transient. It decides whether the record can be
    # called live_verified: an aggregator still listing a role is not the employer confirming
    # it (requirement 5's authority order).
    rank = url_rank(posting.get("_ats"))
    rec["_run_authority"] = min(rank, rec.get("_run_authority", AUTHORITY_AGGREGATOR))
    if not rec.get("requisition_id"):
        rec["requisition_id"] = requisition_id(posting)
    src = posting.get("_source") or posting.get("company", "")
    if src and src not in rec["sources"]:
        rec["sources"].append(src)
    if posting.get("key") and posting["key"] not in rec["source_keys"]:
        rec["source_keys"].append(posting["key"])
    # Prefer the employer/ATS URL over an aggregator's (requirement C).
    if url_rank(posting.get("_ats")) < url_rank(rec.get("canonical_ats")):
        rec["canonical_url"] = posting.get("url") or rec["canonical_url"]
        rec["canonical_ats"] = posting.get("_ats") or rec.get("canonical_ats")
    # Earliest known posting date wins: aggregators re-list, employers post once.
    p = posting.get("posted") or ""
    if p and (not rec.get("posted_date") or p < rec["posted_date"]):
        rec["posted_date"] = p
    # Fill in anything a thinner feed left blank; never overwrite known data with "".
    for field, value in (("compensation", posting.get("salary")),
                         ("employment_type", posting.get("employment_type")),
                         ("expires", posting.get("expires")),
                         ("location", posting.get("location"))):
        if value and not rec.get(field):
            rec[field] = value
    if posting.get("tier") == "core":       # a core match outranks a discovery match
        rec["tier"] = "core"
        rec["demoted"] = False              # ...and clears an earlier demotion
        rec["match_reason"] = posting.get("match_reason") or rec.get("match_reason", "")
    elif not rec.get("tier"):
        rec["tier"] = posting.get("tier") or ""
        rec["demoted"] = bool(posting.get("demoted"))
        rec["match_reason"] = posting.get("match_reason") or ""
    # Once a job is known to come from a priority employer it stays flagged: the same role
    # can also arrive via an aggregator row that carries no flag.
    if posting.get("priority_employer"):
        rec["priority_employer"] = True
        rec["priority_note"] = posting.get("priority_note") or rec.get("priority_note", "")
    if lane and lane not in (rec.get("lane") or ""):
        rec["lane"] = f"{rec['lane']}+{lane}" if rec.get("lane") else lane
    if not rec.get("work_arrangement") or rec["work_arrangement"] == "unknown":
        rec["work_arrangement"] = work_arrangement(posting)
    # A record first seen from a title-only feed gains the technical flag on the day deep
    # retrieval finally supplies its description. Once set it STAYS set: a later appearance
    # from a thinner feed carries no description and must not be read as an all-clear.
    if posting.get("technical_risk"):
        rec["technical_risk"] = True
    return rec


def apply_verdict(rec, verdict, today=None):
    """Copy the model's judgment onto the record and re-derive priority."""
    if not verdict:
        return rec
    score = verdict.get("opportunity_score", verdict.get("score"))
    if score is not None and score >= 0:
        rec["fit_score"] = score
        rec["recommendation"] = verdict.get("recommendation", "")
        rec["confidence"] = verdict.get("confidence", "medium")
        reasons = verdict.get("reasons") or []
        concerns = verdict.get("concerns") or []
        rec["why_fits"] = reasons[0] if reasons else verdict.get("reason", "")
        rec["top_concern"] = concerns[0] if concerns else ""
        # Requirements 3, 4 and 7 travel with the record, not just with the verdict, because
        # the digest and the pre-send QA both read records rather than postings.
        rec["unmet_requirements"] = list(verdict.get("unmet_requirements") or [])
        rec["bridge_skills"] = list(verdict.get("bridge_skills") or [])
        rec["recruiting_gaps"] = list(verdict.get("recruiting_gaps") or [])
        rec["remote_scope"] = verdict.get("remote_scope") or ""
        rec["remote_verified"] = bool(verdict.get("remote_verified"))
        rec["downgraded_from"] = verdict.get("downgraded_from") or ""
    rec["priority"] = priority_for(rec.get("recommendation", ""), band_for(rec, today),
                                   rec.get("confidence", "medium"))
    return rec


def upsert(db, postings, lane, today=None, run_seen=None):
    """Fold this run's postings into the database.

    `run_seen` accumulates the keys touched across EVERY lane of this run. Pass the same set
    to each lane's call: without it, the same job arriving on the local feed and the remote
    feed is counted as two discoveries rather than one job found twice, and the
    "deduplicated across feeds" number the report quotes reads zero.

    Returns counts: discovered (feed appearances offered), new (records created this call),
    duplicates (appearances that merged into a record already touched this run), and the set
    of job_keys seen."""
    today = today or _today()
    jobs = db["jobs"]
    stats = {"discovered": 0, "new": 0, "duplicates": 0, "seen": set(), "revalidated": 0,
             "reopened": 0, "updated": 0}
    already = run_seen if run_seen is not None else set()
    for p in postings:
        stats["discovered"] += 1
        # Identity FIRST (requirement 6). resolve_key tries the composite company+title+
        # location key, then the requisition id, the canonical employer URL and the source
        # key — so a role that changed feeds, picked up a tracking parameter, or had its
        # title punctuation edited resolves to the record it already has instead of being
        # created again as a brand-new discovery.
        key = resolve_key(db, p)
        rec = jobs.get(key) if key else None
        # `fresh`: this posting created the record. `repeat`: the same job already arrived
        # from another feed on this run, so its change state is already decided and must not
        # be recomputed against the record we ourselves just updated.
        fresh, repeat = rec is None, bool(key) and key in already
        if fresh:
            key = dedupe_key(p)
            rec = new_record(p, lane, today)
            jobs[key] = rec
            stats["new"] += 1
        elif repeat:
            stats["duplicates"] += 1
        else:
            before = _material_snapshot(rec)
            # REOPENED means "we confirmed this was closed and it is back". STATUS_REMOVED
            # is the legacy status written by pre-verification runs on nothing more than a
            # feed going quiet — no employer ever confirmed it — so a record carrying it
            # returns as REVALIDATED. Calling that a reopening would over-claim in exactly
            # the way requirement 5 forbids.
            was_closed = (rec.get("status") == STATUS_CLOSED
                          or rec.get("verification") == VERIFIED_CLOSED)
            was_missing = bool(rec.get("missing_since")
                               or rec.get("verification") == NEEDS_RECHECK
                               or rec.get("status") == STATUS_REMOVED)
            if was_closed:      # it left and came back; history is kept, closure is cleared
                rec["closed_date"] = ""
                rec["removal_reason"] = ""
                rec["status"] = STATUS_ACTIVE
            rec["missing_since"] = ""
            rec["verification_reason"] = ""
        _merge_posting(rec, p, lane, today)
        if not fresh and not repeat:
            if was_closed:
                rec["change_state"] = CHANGE_REOPENED
                stats["reopened"] += 1
            elif was_missing:
                rec["change_state"] = CHANGE_REVALIDATED
                stats["revalidated"] += 1
            elif _material_snapshot(rec) != before:
                rec["change_state"] = CHANGE_UPDATED
                stats["updated"] += 1
            else:
                rec["change_state"] = CHANGE_STILL_OPEN
        register_aliases(db, rec, p)
        rec["_posting"] = p                   # transient; stripped before saving
        stats["seen"].add(key)
        already.add(key)
        apply_verdict(rec, p.get("fit_result"), today)
    return stats


def _material_snapshot(rec):
    """The fields whose change makes a record UPDATED rather than STILL OPEN.

    Compared after normalization, so a title that gained a comma or lost "(Remote)" is not a
    change — that is precisely the "punctuation/title wording changed slightly" case
    requirement 6 says must not read as new, and it must not read as updated either."""
    return (norm_title(rec.get("title")), location_bucket(rec.get("location")),
            (rec.get("compensation") or "").strip(),
            canonical_url_key(rec.get("canonical_url")),
            (rec.get("employment_type") or "").strip().lower())


def refresh_statuses(db, seen_keys, today=None, max_age_days=None,
                     exceptional_score=None):
    """Re-derive every record's lifecycle status after an upsert.

    The age rules of requirement D live here, in ONE place:
      0-7 days   -> new / priority
      8-14 days  -> active, still worth applying
      15+ days   -> stale, and therefore not repeated in the digest, UNLESS the role is
                    unusually strong AND was verified as still listed on this run
    A record not seen this run is NOT touched here — deciding that is `close_missing`'s job,
    because only it knows whether the silence came from the source or from a failure."""
    today = today or _today()
    for key, rec in db["jobs"].items():
        if key not in seen_keys:
            continue
        # VERIFICATION (requirement 5), settled before the age bands because a role we cannot
        # authoritatively see is not the same thing as a role that is old.
        #
        # The authority order is the requirement's: an employer/ATS board, or a staffing firm
        # that is itself the applier, listing the role IS the role being live. An aggregator
        # listing it is not — aggregators re-serve cached rows for days after a requisition
        # closes — so an aggregator-only sighting keeps the record in NEEDS_RECHECK until
        # something authoritative confirms it (or `run_verification` checks the URL).
        authority = rec.pop("_run_authority", AUTHORITY_AGGREGATOR)
        rec["source_authority"] = authority
        expired = _expired(rec, today)
        if expired:
            # The posting's own stated deadline has passed. That is the employer speaking, so
            # it is a CLOSED VERIFIED, not a guess — and it costs no request.
            rec["status"] = STATUS_CLOSED
            rec["verification"] = VERIFIED_CLOSED
            rec["verification_reason"] = "expired"
            rec["closed_date"] = rec.get("closed_date") or today
            rec["removal_reason"] = "expired"
            continue
        if authority in AUTHORITATIVE:
            rec["verification"] = VERIFIED_LIVE
            rec["verification_reason"] = ""
            rec["missing_since"] = ""
        else:
            rec["verification"] = NEEDS_RECHECK
            rec["verification_reason"] = "aggregator_only"
        band = band_for(rec, today)
        rec["band"] = band
        if band == BAND_NEW:
            rec["status"] = STATUS_NEW if rec.get("first_seen") == today else STATUS_ACTIVE
        elif band == BAND_APPLY:
            rec["status"] = STATUS_AGING
        else:
            strong = (exceptional_score is not None
                      and (rec.get("fit_score") or -1) >= exceptional_score)
            verified_today = rec.get("verified_open_on") == today
            rec["status"] = STATUS_ACTIVE if (strong and verified_today) else STATUS_STALE
        if max_age_days and (age_days(rec, today) or 0) > max_age_days \
                and rec["status"] == STATUS_STALE:
            rec["aged_out"] = True
        else:
            rec.pop("aged_out", None)
        rec["priority"] = priority_for(rec.get("recommendation", ""), band,
                                       rec.get("confidence", "medium"))


def _expired(rec, today=None):
    """Has the posting's OWN stated deadline passed? A date the employer published is the
    employer telling us the role is closed, so this is the one closure that needs no request."""
    expires = (rec.get("expires") or "")[:10]
    if not expires:
        return False
    days = _days_since(expires, today)
    return days is not None and days > 0


def close_missing(db, seen_keys, failed_sources, today=None):
    """Handle records that did NOT appear this run.

    THE RULE, RESTATED AND NOW ENFORCED (requirement 5): *never mark a role closed solely
    because a feed or aggregator stopped returning it.* Disappearing from a feed is not
    evidence of closure — it is the absence of evidence. So nothing here closes anything.
    A missing record moves to NEEDS_RECHECK and records WHY and SINCE WHEN; only
    `run_verification`, asking the employer's own URL, may move it to CLOSED VERIFIED.

    This is a deliberate narrowing of what this function used to do. It previously wrote
    STATUS_REMOVED — which the digest rendered as "removed" — on nothing more than a feed
    going quiet.

    A record whose every known source FAILED this run is additionally stamped
    `unverified_run`: we did not look, we merely failed to look, and its recheck clock does
    not start. Returns the records newly moved into NEEDS_RECHECK."""
    today = today or _today()
    newly_missing = []
    for key, rec in db["jobs"].items():
        if key in seen_keys or rec.get("verification") == VERIFIED_CLOSED \
                or rec.get("status") in GONE_STATUSES:
            continue
        sources = rec.get("sources") or []
        was_live = rec.get("verification") != NEEDS_RECHECK
        if sources and all(s in failed_sources for s in sources):
            rec["unverified_run"] = today     # audit trail: we could not check this one
            rec["verification"] = NEEDS_RECHECK
            rec["verification_reason"] = "source_failed"
            if was_live:
                newly_missing.append(rec)
            continue
        rec.pop("unverified_run", None)
        rec["verification"] = NEEDS_RECHECK
        rec["verification_reason"] = "not_returned"
        rec["missing_since"] = rec.get("missing_since") or today
        if was_live:
            newly_missing.append(rec)
    return newly_missing


def mark_shown(db, job_keys, today=None):
    """Record that these jobs actually REACHED the reader.

    This is the fix for "jobs in the removed section that were never in an email": the
    removed section is built from `ever_shown`, and only this function sets it.

    Do not call this at render time — call `mark_pending_shown` there and promote with
    `confirm_shown` once the email is known to have gone out. Rendering a report is not the
    same as delivering it (the workflow can skip a recipient, and SMTP can fail), and
    treating the two as equivalent reintroduces the exact bug this field exists to prevent."""
    today = today or _today()
    for key in job_keys:
        rec = db["jobs"].get(key)
        if not rec:
            continue
        if not rec.get("ever_shown"):
            rec["ever_shown"] = True
            rec["first_shown"] = today
        rec["last_shown"] = today
        rec["times_shown"] = int(rec.get("times_shown") or 0) + 1
        rec.pop("pending_shown_on", None)


def mark_pending_shown(db, job_keys, today=None):
    """Record that these jobs are IN a report that has been generated but not yet sent.

    Pending is not shown. If the email never goes out — `chad_only` skipped this person, or
    SMTP failed — the marks simply stay pending, the roles are rendered again on the next
    run, and they still cannot appear in a "removed" section, because nobody has seen them."""
    today = today or _today()
    for key in job_keys:
        rec = db["jobs"].get(key)
        if rec is not None:
            rec["pending_shown_on"] = today


def confirm_shown(db, today=None):
    """Promote this run's pending marks to genuinely shown. Called only after the email for
    this profile has actually been sent. Returns how many records were promoted."""
    today = today or _today()
    pending = [k for k, r in db["jobs"].items() if r.get("pending_shown_on")]
    mark_shown(db, pending, today)
    return len(pending)


def strip_transient(db):
    """Remove the in-memory-only fields before the DB is written to disk."""
    for rec in db["jobs"].values():
        rec.pop("_posting", None)


def prune(db, keep_days=180, today=None):
    """Drop long-closed records so the file cannot grow without bound. Records the human has
    touched (a decision was recorded) are kept regardless — that is the part worth keeping."""
    today = today or _today()
    doomed = []
    for key, rec in db["jobs"].items():
        if rec.get("status") not in GONE_STATUSES or rec.get("user_decision"):
            continue
        gone_for = _days_since(rec.get("closed_date"), today)
        if gone_for is not None and gone_for > keep_days:
            doomed.append(key)
    for key in doomed:
        del db["jobs"][key]
    return len(doomed)


# ---- independent verification (requirement D) ---------------------------------------------

VERIFY_TIMEOUT = 10
VERIFY_OPEN = "open"
VERIFY_CLOSED = "closed"
VERIFY_UNKNOWN = "unknown"

# Text that a live page shows when the requisition behind it is gone. Checked only when the
# URL returns 200, because many ATSes serve a friendly "this job is closed" page rather than
# a 404.
_CLOSED_MARKERS = ("no longer accepting applications", "this job is no longer available",
                   "position has been filled", "job posting is no longer available",
                   "this posting has expired", "job not found", "no longer available")


def verify_open(url, timeout=VERIFY_TIMEOUT, opener=None):
    """Is this posting still live? Returns VERIFY_OPEN / VERIFY_CLOSED / VERIFY_UNKNOWN.

    Deliberately conservative: only a 404/410, or an explicit "no longer available" message
    on the page, counts as CLOSED. Anything else — a timeout, a 403 from a bot wall, a
    redirect — is UNKNOWN, and an UNKNOWN role is never reported as removed. Callers must
    budget these calls; this is the only place the module touches the network."""
    if not url:
        return VERIFY_UNKNOWN
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "prospector/1.0"})
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            body = r.read(20000).decode("utf-8", "replace").lower()
        return VERIFY_CLOSED if any(m in body for m in _CLOSED_MARKERS) else VERIFY_OPEN
    except urllib.error.HTTPError as e:
        return VERIFY_CLOSED if e.code in (404, 410) else VERIFY_UNKNOWN
    except Exception:                             # noqa: BLE001 - timeouts, DNS, TLS, resets
        return VERIFY_UNKNOWN


def verify_candidates(db, seen_keys, limit, exceptional_score, today=None):
    """Pick which old-but-strong roles are worth spending a request on.

    Requirement D says a 15+ day role may only keep appearing if it is unusually strong AND
    independently verified still open. Verification costs a request per role, so this returns
    only the roles that would actually benefit: strong enough to qualify for the exception,
    old enough to need it, and not already verified today."""
    today = today or _today()
    out = []
    for key in seen_keys:
        rec = db["jobs"].get(key)
        if not rec or band_for(rec, today) != BAND_OLD:
            continue
        if (rec.get("fit_score") or -1) < exceptional_score:
            continue
        if rec.get("verified_open_on") == today:
            continue
        out.append(rec)
    out.sort(key=lambda r: -(r.get("fit_score") or 0))
    return out[:limit]


def run_verification(db, seen_keys, limit, exceptional_score, today=None, verifier=None):
    """Verify a bounded number of old-but-strong roles. Returns (checked, closed) counts."""
    today = today or _today()
    verifier = verifier or verify_open
    checked = closed = 0
    for rec in verify_candidates(db, seen_keys, limit, exceptional_score, today):
        result = verifier(rec.get("canonical_url", ""))
        checked += 1
        if result == VERIFY_OPEN:
            rec["verified_open_on"] = today
            rec["last_verified"] = today
            rec["verification"] = VERIFIED_LIVE
            rec["verification_reason"] = ""
        elif result == VERIFY_CLOSED:
            _mark_closed(rec, today, "verified_closed")
            closed += 1
    return checked, closed


def _mark_closed(rec, today, reason):
    rec["status"] = STATUS_CLOSED
    rec["verification"] = VERIFIED_CLOSED
    rec["verification_reason"] = reason
    rec["closed_date"] = today
    rec["removal_reason"] = reason
    rec["missing_since"] = ""
    return rec


def recheck_candidates(db, limit, today=None, min_days=0):
    """Which NEEDS_RECHECK records are worth spending a request on?

    Ordered by what the reader would notice: roles they were actually shown come first (a
    role that appeared in an email and then vanished is the one they will ask about), then
    the strongest scores, then the longest-missing. Records held back because their source
    FAILED are excluded — there is nothing to learn from checking a URL whose feed was simply
    unreachable this morning, and asking would put load on a source already in trouble."""
    today = today or _today()
    out = []
    for rec in db["jobs"].values():
        if rec.get("verification") != NEEDS_RECHECK:
            continue
        if rec.get("verification_reason") == "source_failed":
            continue
        if not rec.get("canonical_url"):
            continue
        missing_days = _days_since(rec.get("missing_since"), today)
        if min_days and (missing_days is None or missing_days < min_days):
            continue
        out.append(rec)
    out.sort(key=lambda r: (0 if r.get("ever_shown") else 1,
                            -(r.get("fit_score") or 0),
                            r.get("missing_since") or ""))
    return out[:limit]


def verify_missing(db, limit, today=None, verifier=None, min_days=0):
    """Ask the EMPLOYER about the roles we can no longer see (requirement 5).

    This is the only route to CLOSED VERIFIED, and the reason the change digest can say a role
    is gone without lying. `verify_open` is conservative — only a 404/410 or an explicit "no
    longer available" counts as closed, and a timeout or a bot wall is UNKNOWN — so a role
    stays in NEEDS_RECHECK indefinitely rather than being written off on a bad connection.

    Returns (checked, newly_closed_records)."""
    today = today or _today()
    verifier = verifier or verify_open
    checked, closed = 0, []
    for rec in recheck_candidates(db, limit, today, min_days):
        # Free closure first: a deadline the posting itself published needs no request.
        if _expired(rec, today):
            closed.append(_mark_closed(rec, today, "expired"))
            continue
        result = verifier(rec.get("canonical_url", ""))
        checked += 1
        if result == VERIFY_CLOSED:
            closed.append(_mark_closed(rec, today, "verified_closed"))
        elif result == VERIFY_OPEN:
            # The feed lost it but the employer still serves the posting. That is exactly the
            # case requirement 5 protects: the aggregator is wrong, the employer is right.
            rec["verification"] = VERIFIED_LIVE
            rec["verification_reason"] = ""
            rec["verified_open_on"] = today
            rec["last_verified"] = today
            rec["missing_since"] = ""
    return checked, closed
