#!/usr/bin/env python3
"""
check_v6.py — the LIVE implementation test for Lisa's V6 update (PROSPECTOR_V6.md, tests A-G).

Runs real, currently-open postings through the PRODUCTION path — classify_match, then
enrich_with_fit (which is score_fit with Lisa's jd_first rubric, enforce_hard_requirements and
apply_remote_eligibility), then the V6 selection — and asserts what Lisa's update requires.

What it touches: a handful of public ATS GETs to fetch the postings, and one Anthropic call per
case (~10 calls). It writes NOTHING: no snapshot, no lifecycle DB, no report, no GITHUB_OUTPUT,
no email. Results print to the terminal and, with --json, to a file of your choosing.

    ANTHROPIC_API_KEY=... python3 check_v6.py
    python3 check_v6.py --offline     # test G and the deterministic halves only, no API

Postings are picked by title from live boards at run time, so a case whose role has closed is
reported as SKIPPED rather than silently passing.
"""

import argparse
import json
import re
import sys

import jobmonitor as jm


def _live(ats, slug, name, title_rx, extra=None):
    """First live posting on a board whose title matches `title_rx`, or None."""
    cfg = {"name": name, "ats": ats, "slug": slug, **(extra or {})}
    try:
        postings = jm.FETCHERS[ats](cfg)
    except Exception as e:                       # a closed board is a skip, not a crash
        print(f"  [skip] {name}: {type(e).__name__}: {str(e)[:60]}")
        return None
    for p in postings:
        if re.search(title_rx, p["title"], re.I):
            return p
    return None


# (test id, what it proves, how to find the posting live). Every posting is real.
CASES = [
    ("A", "travel employer recognised as DIRECT industry experience",
     lambda: _live("ashby", "perk", "Perk (TravelPerk)", r"director.*customer care|customer care.*director")),
    ("A", "travel employer recognised as DIRECT industry experience",
     lambda: _live("greenhouse", "airbnb", "Airbnb", r"^programs? (&|and) business operations lead")),
    ("B", "title matches, the work is narrow sales operations",
     lambda: _live("greenhouse", "airbnb", "Airbnb", r"senior sales operations lead")),
    ("C", "unconventional title, transferable work",
     lambda: _live("greenhouse", "tripadvisor", "Tripadvisor", r"customer experience improvement lead")),
    ("D", "consultative technical-sales role",
     lambda: _live("atlassian", "atlassian", "Atlassian", r"solutions? consultant",
                   {"domain": "atlassian.com"})),
    ("F", "superficially matching title, specialist regulatory work",
     lambda: _live("greenhouse", "airbnb", "Airbnb", r"regulatory operations lead")),
    ("F", "superficially matching title, narrow deal-desk work",
     lambda: _live("ashby", "perk", "Perk (TravelPerk)", r"deal desk")),
]

# Test B's second half: the SAME title with two different JDs. These two descriptions are
# written for the test (labelled as such in the output) so the only variable is the work.
_B_TITLE = "Director, Operations"
_B_GOOD = ("Lead enterprise operating-model redesign after our recent acquisition: own the "
           "integration management office, drive change management and organizational design "
           "across three business units, and report to the COO on value realization.")
_B_BAD = ("Run day-to-day call-center operations for a 400-seat inbound support floor: "
          "schedule agents, manage average handle time, staff shifts, monitor queue SLAs, "
          "and handle escalations. Must have 8+ years running contact-center floors.")

NO_SALES = re.compile(r"\bno (prior |direct |real )?sales (experience|background)\b|"
                      r"\blacks? (any )?sales experience\b|\bwithout (any )?sales experience\b",
                      re.I)
NEW_TRAVEL = re.compile(r"\b(new|no|never|unfamiliar)\b[^.]{0,40}\b(travel|aviation|airline)"
                        r"\b|\b(travel|aviation)\b[^.]{0,30}\bnew industry\b", re.I)
ADOBE_DONE = re.compile(r"(completed|finished|graduated|certified|holds?)[^.]{0,40}adobe|"
                        r"adobe[^.]{0,60}(completed|certified|certification earned)", re.I)


def _text(v):
    keys = ("why_fits", "material_gap", "next_action", "dominant_work", "exception_reason")
    lists = ("reasons", "concerns", "cautions", "unmet_requirements", "key_qualifications")
    return " ".join([str(v.get(k) or "") for k in keys]
                    + [" ".join(v.get(k) or []) for k in lists])


def _score(postings, profile, client):
    """The production scoring path, verbatim: enrich_with_fit with Lisa's real profile."""
    jm.enrich_with_fit(postings, None, profile, client)
    return [p.get("fit_result") or {} for p in postings]


def test_g():
    """Fewer than five: two worthwhile roles in, two out — never padded to five. And zero."""
    def rec(key, **kw):
        base = {"job_key": key, "title": key, "company": "X", "recommendation": "strong_fit",
                "work_interest": "A", "fit_score": 80, "status": "new", "ever_shown": False}
        base.update(kw)
        return base
    jobs = {"good1": rec("good1"), "good2": rec("good2", fit_score=75),
            "meh1": rec("meh1", recommendation="stretch", fit_score=60),
            "meh2": rec("meh2", recommendation="stretch", work_interest="C", fit_score=55),
            "bad": rec("bad", recommendation="not_recommended", fit_score=20),
            "avoid": rec("avoid", work_interest="D", fit_score=70)}
    profile = next(p for p in json.load(open(jm.PROFILES))["profiles"] if p["name"] == "lisa")
    sections, counts = jm.change_sections({"jobs": jobs}, None, seen_keys=set(jobs),
                                          profile=profile)
    picked = [r["job_key"] for k in jm.NEW_SECTIONS for r in sections.get(k) or []]
    sections0, _ = jm.change_sections({"jobs": {"meh1": jobs["meh1"]}}, None,
                                      seen_keys={"meh1"}, profile=profile)
    zero = sum(len(sections0.get(k) or []) for k in jm.NEW_SECTIONS)
    ok = sorted(picked) == ["good1", "good2"] and zero == 0
    return ok, f"6 candidates -> {picked} (cap 5, not padded); stretch-only day -> {zero} shown"


def test_e_static(candidate):
    a = candidate.get("adobe_digital_academy") or {}
    ok = a.get("status") == "in_progress" and "December 2026" in a.get("expected_completion", "")
    return ok, f"candidate context: status={a.get('status')!r}, expected={a.get('expected_completion')!r}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", help="skip the API cases")
    ap.add_argument("--json", help="also write full verdicts to this file")
    args = ap.parse_args(argv)

    profile = next(p for p in json.load(open(jm.PROFILES))["profiles"] if p["name"] == "lisa")
    candidate = jm.load_background(profile)
    results = []

    def report(test, ok, evidence):
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        results.append({"test": test, "result": mark, "evidence": evidence})
        print(f"[{mark}] Test {test}: {evidence}")

    ok, ev = test_g()
    report("G", ok, ev)
    ok, ev = test_e_static(candidate)
    report("E (context)", ok, ev)
    if args.offline:
        return 0 if all(r["result"] != "FAIL" for r in results) else 1

    client = jm.get_client()
    if client is None:
        print("ANTHROPIC_API_KEY is not set or the anthropic package is missing — "
              "run with --offline, or set the key.")
        return 2

    dump = []
    for test, purpose, find in CASES:
        p = find()
        if not p:
            report(test, None, f"{purpose}: no matching live posting today")
            continue
        tier, reason = jm.classify_match(p, profile)
        label = f'{p["company"]} — {p["title"]}'
        if not tier:
            # Dropped before scoring is a legitimate outcome for B/F (and costs nothing).
            ok = test in ("B", "F")
            report(test, ok, f"{label}: dropped before scoring ({reason})")
            continue
        p["tier"], p["match_reason"] = tier, reason
        v = _score([p], profile, client)[0]
        if not v.get("valid"):
            report(test, False, f"{label}: scoring failed ({v.get('reason')})")
            continue
        dump.append({"test": test, "posting": label, "url": p.get("url"), "verdict": v})
        text = _text(v)
        rec, interest = v.get("recommendation"), v.get("work_interest")
        summary = (f'{label}: {rec}, interest {interest}, fit {v.get("fit_level")}, '
                   f'industry {v.get("industry_relationship")}, status {v.get("action_status")}')
        if test == "A":
            ok = v.get("industry_relationship") == "direct" and not NEW_TRAVEL.search(text)
            report("A", ok, summary)
        elif test == "B":
            ok = rec in ("stretch", "not_recommended") or interest == "D"
            report("B", ok, summary + f' — dominant work: {v.get("dominant_work")!r}')
        elif test == "C":
            states = " ".join(v.get("key_qualifications") or [])
            ok = rec != "not_recommended" and ("TRANSFERABLE" in states or "CONFIRMED" in states)
            report("C", ok, summary + f" — {states[:120]}")
        elif test == "D":
            ok = (v.get("sales_readiness") not in ("", "n_a") and not NO_SALES.search(text))
            report("D", ok, summary + f' — sales readiness {v.get("sales_readiness")}')
            report("E", not ADOBE_DONE.search(text),
                   f'{label}: training described as {"COMPLETED (wrong)" if ADOBE_DONE.search(text) else "in progress / not claimed complete"}'
                   f' — "{(v.get("why_fits") or "")[:110]}"')
        elif test == "F":
            ok = rec in ("stretch", "not_recommended") or interest == "D"
            report("F", ok, summary)

    # Test B, second half: same title, the JD alone changes the verdict.
    pair = [jm._norm("Acme (test JD)", f"b{i}", _B_TITLE, "Remote, United States", "",
                     "2026-10-27", description=d) for i, d in enumerate((_B_GOOD, _B_BAD))]
    for p in pair:
        p["tier"], p["match_reason"] = "core", "core title gate"
    good, bad = _score(pair, profile, client)
    if good.get("valid") and bad.get("valid"):
        ok = good["opportunity_score"] > bad["opportunity_score"] and \
            bad["recommendation"] in ("stretch", "not_recommended")
        report("B (same title, two JDs)", ok,
               f'"{_B_TITLE}" — integration/op-model JD: {good["recommendation"]} '
               f'({good.get("work_interest")}); call-center JD: {bad["recommendation"]} '
               f'({bad.get("work_interest")})')
        dump += [{"test": "B-pair", "verdict": good}, {"test": "B-pair", "verdict": bad}]
    print(jm.fit_usage_summary())
    if args.json:
        json.dump({"results": results, "verdicts": dump}, open(args.json, "w"), indent=2)
    return 0 if all(r["result"] != "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
