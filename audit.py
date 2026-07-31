#!/usr/bin/env python3
"""
audit.py — weekly review pass over Prospector's own behavior.

Run:  python3 audit.py                 # audit every enabled profile
      python3 audit.py --profile lisa

Writes AUDIT_<name>.md (committed by the weekly workflow).

IT MAKES NO NETWORK CALLS AT ALL. No ATS fetch, no Anthropic API. Everything it reports is
read from files the daily run already committed:

    snapshot_<name>*.json   what is currently tracked, with each role's verdict
    rejects_<name>.json     roles fetched but filtered out, with the rule that dropped them
                            (last 10 days, leadership-shaped titles only)
    feedback_<name>.json    your hand-entered feedback
    source_health.json      per-company roles returned + consecutive zero-result runs

That design is deliberate. The daily run already computes the filtered-out set, so capturing
it costs nothing, we stay inside the project's "one run per day, don't hammer ATS endpoints"
rule, and a weekly audit sees a FULL WEEK of data instead of one morning's fetch.

Nothing here changes scoring policy, the profile, or any snapshot. It reports; a human
decides. Learning stays transparent and reversible.
"""
import argparse
import collections
import datetime
import json
import os
import re

import jobmonitor as jm

HERE = jm.HERE
TOP_N = 15


def _read(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _profiles(only=None):
    profiles = _read(jm.PROFILES, {}).get("profiles", [])
    if only:
        profiles = [p for p in profiles if p["name"] == only]
    return [p for p in profiles if p.get("enabled", True)]


def _snapshots(name, directory):
    """[(lane_label, [roles])] for every lane snapshot this profile has."""
    out = []
    for suffix, label in (("", "Local"), ("_remote", "US-Remote"),
                          ("_staffing", "Contract/Staffing")):
        path = os.path.join(directory, f"snapshot_{name}{suffix}.json")
        if os.path.exists(path):
            out.append((label, _read(path, [])))
    return out


def _current_picture(lanes):
    tally, not_rec, unscored, tracked = collections.Counter(), [], [], 0
    for label, roles in lanes:
        for r in roles:
            tracked += 1
            v = r.get("fit_result") or {}
            rec = v.get("recommendation")
            if not rec:
                tally["unscored"] += 1
                unscored.append((r, label))
                continue
            tally[rec] += 1
            if rec == "not_recommended":
                not_rec.append((v.get("opportunity_score", -1), r, label))
    return tally, not_rec, unscored, tracked


def _reject_patterns(rejects_doc):
    """Aggregate the reject reasons across the retained days."""
    by_reason = collections.Counter()
    by_role = collections.OrderedDict()
    days = 0
    for day in rejects_doc.get("days", []):
        days += 1
        for lane_title, rows in (day.get("lanes") or {}).items():
            for row in rows:
                by_reason[row.get("reason", "?")] += 1
                key = (row.get("company", ""), row.get("title", ""))
                if key not in by_role:
                    by_role[key] = {"reason": row.get("reason", ""),
                                    "location": row.get("location", ""),
                                    "lane": lane_title, "days": 0}
                by_role[key]["days"] += 1
    return days, by_reason, by_role


def _false_positive_patterns(feedback):
    words, rows = collections.Counter(), []
    for src in ("by_key", "by_ident"):
        for ident, rec in feedback[src].items():
            if rec["status"] in jm.FALSE_POSITIVE_STATUSES:
                title = ident.split("::")[-1]
                rows.append((title, rec["status"]))
                for w in re.findall(r"[a-z&]{4,}", title.lower()):
                    words[w] += 1
    return rows, words


def audit_profile(profile, directory):
    name = profile["name"]
    L = [f"# Prospector audit — {profile['label']}", "",
         f"_Generated {datetime.date.today().isoformat()} by `audit.py` from committed files "
         f"only. No ATS fetch, no Anthropic API call, nothing modified._", ""]

    lanes = _snapshots(name, directory)
    tally, not_rec, unscored, tracked = _current_picture(lanes)

    # ---- 1. what is currently tracked ----
    L += ["## Current picture", "", f"- Roles tracked across all lanes: **{tracked}**"]
    for rec in list(jm.RECOMMENDATIONS) + ["unscored"]:
        if tally.get(rec):
            L.append(f"- `{rec}`: **{tally[rec]}**")
    for label, roles in lanes:
        L.append(f"- {label}: {len(roles)}")
    L.append("")

    # ---- 2. possible false negatives ----
    L += ["## Possible false negatives",
          "_Highest-scoring roles the model still marked `not_recommended`. If any of these "
          "look right to you, the scoring profile or the prompt may be too strict._", ""]
    if not_rec:
        for score, r, label in sorted(not_rec, key=lambda x: -(x[0] or -1))[:TOP_N]:
            why = (r.get("fit_result") or {}).get("reason", "")
            L.append(f"- **{r['company']}** — {r['title']} · {label} · score {score}"
                     + (f" · _{why}_" if why else ""))
    else:
        L.append("_None._")
    L.append("")

    if unscored:
        L += ["### Roles that could not be scored",
              "_Scoring failed or was off for these. They are still shown in the email, but "
              "unranked._", ""]
        for r, label in unscored[:TOP_N]:
            L.append(f"- **{r['company']}** — {r['title']} · {label}")
        L.append("")

    # ---- 3. fetched but filtered out ----
    rejects_doc = _read(os.path.join(directory, f"rejects_{name}.json"), {})
    days, by_reason, by_role = _reject_patterns(rejects_doc)
    L += ["## Fetched but filtered out",
          f"_Leadership-shaped titles the filter dropped, across the last {days} recorded "
          f"run(s). Recorded by the daily run, so this needs no extra fetching. Use it to "
          f"spot an over-tight exclusion._", ""]
    if by_role:
        L.append(f"**{len(by_role)}** distinct role(s) rejected. Most common rules:")
        for reason, n in by_reason.most_common(10):
            L.append(f"- {reason} — {n} occurrence(s)")
        L.append("")
        L.append("| Company | Title | Rule that dropped it | Runs |")
        L.append("|---|---|---|---|")
        ordered = sorted(by_role.items(), key=lambda kv: -kv[1]["days"])
        for (company, title), meta in ordered[:40]:
            L.append(f"| {company} | {title} | {meta['reason']} | {meta['days']} |")
        if len(ordered) > 40:
            L.append(f"\n_…and {len(ordered) - 40} more._")
    else:
        L.append("_Nothing recorded yet. This fills in after the next daily run._")
    L.append("")

    # ---- 4. false positives from feedback ----
    feedback = jm.load_feedback(name, directory=directory)
    rows, words = _false_positive_patterns(feedback)
    L += ["## Repeated false-positive patterns", ""]
    if rows:
        L.append(f"{len(rows)} role(s) you marked "
                 f"`{'`, `'.join(sorted({s for _, s in rows}))}`:")
        for title, status in rows[:20]:
            L.append(f"- {title} → `{status}`")
        common = [f"`{w}` ×{n}" for w, n in words.most_common(8) if n > 1]
        if common:
            L += ["", "Recurring words in rejected titles: " + ", ".join(common), "",
                  "_A word recurring here is a candidate for `exclude_any` — but check it "
                  "does not also appear in roles you want to keep seeing._"]
    else:
        L.append(f"_No feedback recorded yet. Mark a few roles in `feedback_{name}.json` "
                 f"and this section becomes the most useful one here._")
    L.append("")
    return "\n".join(L)


def audit_sources(directory):
    health = _read(os.path.join(directory, "source_health.json"), {})
    rows = health.get("sources", [])
    L = ["## Source health", ""]
    if not rows:
        L += ["_No source_health.json yet — it is written by the next daily run._", ""]
        return "\n".join(L)
    L.append(f"_From the run on {health.get('generated', '?')}._")
    L.append("")
    failed = [r for r in rows if r.get("fetch_failed")]
    if failed:
        L += ["### Sources that FAILED to respond on the last run", ""]
        for r in failed:
            L.append(f"- **{r['name']}** ({r['ats']}/{r['slug']}) — roles from this company "
                     f"were held, not reported as removed")
        L.append("")
    zero = sorted((r for r in rows if not r["roles_returned"] and not r.get("fetch_failed")),
                  key=lambda r: -r.get("consecutive_zero_runs", 0))
    L += ["### Producing no results", ""]
    if zero:
        L.append("| Company | Registry | ATS / slug | Runs with zero results |")
        L.append("|---|---|---|---|")
        for r in zero:
            streak = r.get("consecutive_zero_runs", 0)
            flag = " ⚠️" if streak >= 5 else ""
            L.append(f"| {r['name']} | {r['registry']} | {r['ats']}/{r['slug']} "
                     f"| {streak}{flag} |")
        L += ["", "_A company returning zero for many consecutive runs usually means an ATS "
                  "migration or a changed slug — worth checking its careers page. A ⚠️ marks "
                  "5+ runs._"]
    else:
        L.append("_Every configured source returned at least one role._")
    L.append("")
    producing = len(rows) - len(zero) - len(failed)
    L.append(f"**{producing}/{len(rows)}** configured sources produced roles on the last run.")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(
        description="Weekly self-audit for Prospector. Reads committed files only — "
                    "makes no network calls and changes no state.")
    ap.add_argument("--profile", metavar="NAME", help="audit one profile")
    ap.add_argument("--in-dir", metavar="DIR",
                    help="where to read snapshots/rejects/feedback (default: repo root)")
    ap.add_argument("--out-dir", metavar="DIR", help="where to write AUDIT_*.md")
    args = ap.parse_args()
    in_dir = args.in_dir or HERE
    out_dir = args.out_dir or in_dir

    profiles = _profiles(args.profile)
    if not profiles:
        raise SystemExit("no enabled profiles to audit")
    sources = audit_sources(in_dir)
    for prof in profiles:
        body = audit_profile(prof, in_dir) + sources
        path = os.path.join(out_dir, f"AUDIT_{prof['name']}.md")
        open(path, "w").write(body)
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
