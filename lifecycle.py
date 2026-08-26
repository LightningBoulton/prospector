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
STATUS_REMOVED = "removed"  # no longer listed by its source (we cannot prove why)

ACTIVE_STATUSES = (STATUS_NEW, STATUS_ACTIVE, STATUS_AGING)
GONE_STATUSES = (STATUS_CLOSED, STATUS_REMOVED)

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
        return {"version": DB_VERSION, "jobs": {}}
    try:
        raw = json.load(open(path))
    except Exception as e:                       # noqa: BLE001
        print(f"[warn] jobs_{profile_name}.json unreadable ({type(e).__name__}); "
              f"starting a fresh discovery database.")
        return {"version": DB_VERSION, "jobs": {}}
    jobs = raw.get("jobs")
    if not isinstance(jobs, dict):
        return {"version": DB_VERSION, "jobs": {}}
    return {"version": raw.get("version", DB_VERSION), "jobs": jobs}


def save_db(db, profile_name, directory):
    path = db_path(profile_name, directory)
    doc = {"_comment": (
        "Prospector's persistent job discovery database — one record per real job, keyed by "
        "company+title+location so the same role found on several feeds is ONE row. This is "
        "state: it carries first_seen, ever_shown and the human decision fields, and the "
        "daily digest's 'removed' section is derived from it. Do not delete casually."),
        "version": DB_VERSION, "generated": _today(),
        "decisions": list(DECISIONS), "rejection_reasons": list(REJECTION_REASONS),
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
        # An employer the reader singled out (registry `priority: true`). Deliberately NOT
        # called `priority` — that key below is the P1/P2/P3 urgency band.
        "priority_employer": bool(posting.get("priority_employer")),
        "priority_note": posting.get("priority_note") or "",
        "demoted": bool(posting.get("demoted")),
        "match_reason": posting.get("match_reason") or "",
        "lane": lane,
        "status": STATUS_NEW,
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


def _merge_posting(rec, posting, lane, today):
    """Fold one feed appearance into an existing record."""
    rec["last_seen"] = today
    rec["last_verified"] = today
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
    stats = {"discovered": 0, "new": 0, "duplicates": 0, "seen": set()}
    already = run_seen if run_seen is not None else set()
    for p in postings:
        key = dedupe_key(p)
        stats["discovered"] += 1
        rec = jobs.get(key)
        if rec is None:
            rec = new_record(p, lane, today)
            jobs[key] = rec
            stats["new"] += 1
        else:
            if key in already:
                stats["duplicates"] += 1      # this job already arrived from another feed
            # A record that had left and came back is active again, but keeps its history.
            if rec.get("status") in GONE_STATUSES:
                rec["closed_date"] = ""
                rec["removal_reason"] = ""
        _merge_posting(rec, p, lane, today)
        rec["_posting"] = p                   # transient; stripped before saving
        stats["seen"].add(key)
        already.add(key)
        apply_verdict(rec, p.get("fit_result"), today)
    return stats


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


def close_missing(db, seen_keys, failed_sources, today=None):
    """Mark records that did NOT appear this run.

    THE RULE THAT MATTERS (requirement D): a crawler or feed error must never, by itself,
    mark a job removed. A record whose every known source failed this run is left completely
    alone — status untouched, no removal date — because we did not look, we merely failed to
    look. Returns the records newly marked gone."""
    today = today or _today()
    newly_gone = []
    for key, rec in db["jobs"].items():
        if key in seen_keys or rec.get("status") in GONE_STATUSES:
            continue
        sources = rec.get("sources") or []
        if sources and all(s in failed_sources for s in sources):
            rec["unverified_run"] = today     # audit trail: we could not check this one
            continue
        rec.pop("unverified_run", None)
        rec["status"] = STATUS_REMOVED
        rec["closed_date"] = today
        rec["removal_reason"] = rec.get("removal_reason") or "not_listed"
        newly_gone.append(rec)
    return newly_gone


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
        elif result == VERIFY_CLOSED:
            rec["status"] = STATUS_CLOSED
            rec["closed_date"] = today
            rec["removal_reason"] = "verified_closed"
            closed += 1
    return checked, closed
