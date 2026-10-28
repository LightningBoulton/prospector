#!/usr/bin/env python3
"""
qa.py — the pre-send quality gate.

Everything upstream of this module is a pipeline: retrieve, screen, score, band, select. This
module is the last thing that happens before the email is rendered, and it asks the eight
questions the requirement asks, over the roles that are ACTUALLY about to be shown rather than
over the whole inventory.

WHY A SEPARATE PASS, WHEN EVERY RULE IS ALREADY ENFORCED UPSTREAM
The upstream rules are enforced per role, in isolation, at the moment that role is judged. The
failures this catches are properties of the SELECTION:

  * "Is the #1 role genuinely executable?" is a question about the top of a sorted list. No
    per-role rule can answer it, because no per-role rule knows which role is first.
  * "Are any 'new' roles actually repeats?" is a question about a section, not a record.
  * "Were strong SEO/content opportunities included?" is a question about what is ABSENT, and
    absence is invisible to a rule that only ever runs on things that are present.

It is also a genuine backstop. Every check here duplicates an upstream guarantee on purpose:
if a cached verdict, a config edit or a future change lets something slip, the failure is
caught with the email still unsent, and it is recorded rather than silently repaired.

WHAT IT DOES ON A FAILURE
Downgrades, never deletes: a failing role moves to a lower section (wildcards, or out of the
removed list), and the reason is recorded in the returned findings. The findings are printed
on every run and rendered into the email, so a check that starts failing every day is visible
rather than quietly reshaping the report.
"""

import lifecycle
import screening

# A role may not lead the email — or sit in Apply First at all — with any of these attached.
# They are the machine-readable form of "is the #1 role genuinely executable without
# unsupported technical expertise".
BLOCKING_FLAGS = ("unmet_requirements", "recruiting_gaps")


def _title(rec):
    return f'{rec.get("title") or "(untitled)"} — {rec.get("company") or ""}'.strip(" —")


def _finding(check, verdict, detail, roles=()):
    return {"check": check, "verdict": verdict, "detail": detail,
            "roles": [_title(r) for r in roles][:5]}


def review(sections, db, profile, seen_keys=None, counts=None):
    """Run the eight pre-send checks. Returns (sections, findings).

    `sections` is mutated and returned so the caller renders the CORRECTED selection — a check
    that only reported a problem it could have fixed would be theatre."""
    findings = []
    seen_keys = seen_keys or set()
    counts = counts or {}

    _check_top_role_executable(sections, findings)
    _check_hard_requirements(sections, findings)
    _check_new_means_new(sections, findings)
    _check_removed_are_verified_closed(sections, findings)
    _check_employer_source_wins(sections, findings)
    _check_remote_eligibility(sections, findings)
    _check_seo_coverage(sections, db, profile, seen_keys, findings)
    _check_recruiting_judged_on_duties(db, profile, seen_keys, findings)
    return sections, findings


# ---- 1. is the #1 role genuinely executable? -----------------------------------------------

def _check_top_role_executable(sections, findings):
    """The headline check. Apply First is the section the reader acts on first, so a role with
    an unsupported hard requirement or a technical-execution flag must not be in it at all —
    and the role at position one least of all."""
    keep, moved = [], []
    for rec in sections.get("apply_first") or []:
        blocked = any(rec.get(flag) for flag in BLOCKING_FLAGS) or rec.get("technical_risk")
        if blocked and rec.get("recommendation") == "apply_first":
            moved.append(rec)
        else:
            keep.append(rec)
    if moved:
        sections["apply_first"] = keep
        # Wildcards, not deletion: the role is still worth a human's eyes, it is simply not
        # something to act on first.
        sections.setdefault("wildcards", [])
        sections["wildcards"] = (sections["wildcards"] + moved)[:6]
        findings.append(_finding(
            "top role executable", "corrected",
            f"{len(moved)} role(s) left Apply First: an unsupported requirement or technical "
            f"execution work", moved))
    elif keep:
        findings.append(_finding("top role executable", "pass",
                                 f"lead role: {_title(keep[0])}"))
    else:
        findings.append(_finding("top role executable", "pass",
                                 "nothing in Apply First today"))


# ---- 2. did any hard requirement get ignored? ------------------------------------------------

def _check_hard_requirements(sections, findings):
    """A backstop for `enforce_hard_requirements`. Anything still recommended above `stretch`
    while carrying an unmet requirement means enforcement did not run on it — most likely a
    verdict cached under an older schema."""
    offenders = [r for name in ("apply_first", "new_review", "still")
                 for r in sections.get(name) or []
                 if r.get("unmet_requirements")
                 and r.get("recommendation") in ("apply_first", "strong_fit")]
    if not offenders:
        findings.append(_finding("hard requirements", "pass",
                                 "no role above 'stretch' carries an unmet requirement"))
        return
    for rec in offenders:
        rec["recommendation"] = "stretch"
        rec["downgraded_from"] = rec.get("downgraded_from") or "strong_fit"
    findings.append(_finding("hard requirements", "corrected",
                             f"{len(offenders)} role(s) capped at 'stretch' at send time",
                             offenders))


# ---- 3. are any "new" roles actually repeats? ------------------------------------------------

def _check_new_means_new(sections, findings):
    """Requirement 6: NEW means genuinely never reported before. A role the reader has already
    been emailed is a repeat however it re-entered the pipeline — a second source found it, a
    tracking parameter changed, or it briefly dropped out of a feed and came back."""
    repeats = []
    for name in ("apply_first", "new_review"):
        keep = []
        for rec in sections.get(name) or []:
            if rec.get("ever_shown"):
                repeats.append(rec)
                rec["change_state"] = rec.get("change_state") or lifecycle.CHANGE_STILL_OPEN
                sections.setdefault("still", []).append(rec)
            else:
                keep.append(rec)
        sections[name] = keep
    if repeats:
        findings.append(_finding(
            "new means new", "corrected",
            f"{len(repeats)} previously-emailed role(s) moved out of the new sections",
            repeats))
    else:
        findings.append(_finding("new means new", "pass",
                                 "every role in the new sections is a first report"))


# ---- 4. are any "removed" roles only missing from a feed? ------------------------------------

def _check_removed_are_verified_closed(sections, findings):
    """Requirement 5, at the last possible moment. Only a CLOSED VERIFIED role may be reported
    as gone; a role that is merely NEEDS RECHECK has not been shown to be closed by anyone."""
    keep, dropped = [], []
    for rec in sections.get("removed") or []:
        if rec.get("verification") == lifecycle.VERIFIED_CLOSED:
            keep.append(rec)
        else:
            dropped.append(rec)
    sections["removed"] = keep
    if dropped:
        findings.append(_finding(
            "removed are verified closed", "corrected",
            f"{len(dropped)} role(s) held back: not visible right now, but no employer "
            f"confirmation of closure", dropped))
    else:
        findings.append(_finding("removed are verified closed", "pass",
                                 f"{len(keep)} removal(s), all confirmed at the employer"))


# ---- 5. did the employer source override aggregators? ----------------------------------------

def _check_employer_source_wins(sections, findings):
    """Requirement 5's authority order, checked where it is visible to the reader: the apply
    link. A record that has ever been seen on an employer or ATS board should be linking there,
    not at an aggregator that may redirect, expire, or land on a search page."""
    weak = [r for name in ("apply_first", "new_review", "wildcards", "still")
            for r in sections.get(name) or []
            if lifecycle.url_rank(r.get("canonical_ats")) >= lifecycle.AUTHORITY_AGGREGATOR
            and len(r.get("sources") or []) > 1]
    if weak:
        findings.append(_finding(
            "employer source wins", "warn",
            f"{len(weak)} role(s) link to an aggregator despite being seen on more than one "
            f"source", weak))
    else:
        findings.append(_finding("employer source wins", "pass",
                                 "every multi-source role links to its strongest source"))


# ---- 6. is remote eligibility actually supported by the JD? ----------------------------------

def _check_remote_eligibility(sections, findings):
    """Requirement 7. The report may call a role nationwide-US remote only where the posting
    says so; anything else is relabelled to what we actually know."""
    revoked, unstated = [], 0
    for name in ("apply_first", "new_review", "wildcards", "still"):
        for rec in sections.get(name) or []:
            if rec.get("work_arrangement") != "remote":
                continue
            if rec.get("remote_scope") == screening.SCOPE_NATIONWIDE and rec.get(
                    "remote_verified"):
                continue
            # A claim we are about to withdraw is a correction worth reporting. A posting
            # that simply never said is the ordinary case — counted, not itemised, because a
            # daily "28 corrected" line for the normal state trains the reader to skip the
            # whole block.
            if rec.get("remote_verified"):
                revoked.append(rec)
            else:
                unstated += 1
            rec["remote_verified"] = False
            if rec.get("remote_scope") in ("", None):
                rec["remote_scope"] = screening.SCOPE_UNCLEAR
    if revoked:
        findings.append(_finding(
            "remote eligibility from the JD", "corrected",
            f"{len(revoked)} nationwide-remote claim(s) withdrawn: the posting does not "
            f"support them", revoked))
    else:
        findings.append(_finding(
            "remote eligibility from the JD", "pass",
            f"no unsupported nationwide-remote claim"
            + (f"; {unstated} remote role(s) labelled with what the posting actually says"
               if unstated else "")))


# ---- 7. were strong SEO/content opportunities included? --------------------------------------

def _check_seo_coverage(sections, db, profile, seen_keys, findings):
    """Requirement 8, asked as a question about ABSENCE — the only check here that looks
    outside the selection, because a missing role cannot be found by inspecting the roles that
    are present.

    A strong SEO/content role that was retrieved and scored but did not make the cut is
    promoted rather than merely reported: the requirement asks for stronger coverage of exactly
    these roles, so losing one to a section quota is the failure it names."""
    shown = {r.get("job_key") for name in sections for r in sections.get(name) or []}
    candidates = []
    for key in seen_keys:
        rec = (db.get("jobs") or {}).get(key)
        if not rec or rec.get("job_key") in shown:
            continue
        if not screening.is_seo_content({"title": rec.get("title")}, profile):
            continue
        if rec.get("recommendation") in ("", "not_recommended"):
            continue
        if rec.get("status") == lifecycle.STATUS_STALE or rec.get("ever_shown"):
            continue
        candidates.append(rec)
    if not candidates:
        findings.append(_finding("SEO / content coverage", "pass",
                                 "no scored SEO or content role was left out"))
        return
    candidates.sort(key=lambda r: -(r.get("fit_score") or 0))
    promoted = candidates[:3]
    sections.setdefault("new_review", [])
    sections["new_review"] = sections["new_review"] + promoted
    findings.append(_finding(
        "SEO / content coverage", "corrected",
        f"{len(promoted)} SEO/content role(s) promoted into the email that a section quota "
        f"had cut", promoted))


# ---- 8. were talent/recruiting roles judged on duties? ---------------------------------------

def _check_recruiting_judged_on_duties(db, profile, seen_keys, findings):
    """Requirement 4. A recruiting role screened out as not_recommended must have a REASON
    recorded — an unmet requirement or a named execution gap. Screened out with no evidence is
    the title-history judgment the requirement forbids, so it is reported for a human."""
    unexplained = []
    for key in seen_keys:
        rec = (db.get("jobs") or {}).get(key)
        if not rec or rec.get("recommendation") != "not_recommended":
            continue
        if not screening.is_recruiting_role({"title": rec.get("title")}, profile):
            continue
        if rec.get("unmet_requirements") or rec.get("recruiting_gaps") \
                or rec.get("top_concern"):
            continue
        unexplained.append(rec)
    if unexplained:
        findings.append(_finding(
            "recruiting judged on duties", "warn",
            f"{len(unexplained)} recruiting/talent role(s) were screened out with no stated "
            f"requirement or execution gap — check these are not title-history judgments",
            unexplained))
    else:
        findings.append(_finding("recruiting judged on duties", "pass",
                                 "every screened-out recruiting role names its reason"))


# ---- reporting -------------------------------------------------------------------------------

_MARK = {"pass": "✓", "corrected": "✎", "warn": "!"}


def summary_text(findings):
    lines = ["--- pre-send QA ---"]
    for f in findings:
        lines.append(f"  {_MARK.get(f['verdict'], '?')} {f['check']}: {f['detail']}")
        for role in f["roles"]:
            lines.append(f"      · {role}")
    return "\n".join(lines)


def corrected(findings):
    return [f for f in findings if f["verdict"] in ("corrected", "warn")]
