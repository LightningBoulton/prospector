# Prospector V2 — Changelog

Rollback point: git tag **`v1-pre-v2`** (see [Rollback](#rollback)).

Everything below preserves the original architecture: one Python engine, standard library
only (plus the pre-existing optional `anthropic`), no database, GitHub Actions unchanged as
the runner. **Chad's profile and email are functionally unchanged** — verified by a test that
diffs his committed profile against the pre-V2 tag.

---

## 1. Lisa's job matching (WS1)

**Why:** her filter was simultaneously too loose and too tight. The function group contained
bare words — `experience`, `support`, `service`, `care`, `people`, `customer`, `client`,
`delivery`, `success`, `community`, `innovation`, `AI` — so any Manager/Lead title carrying
one of them passed ("Support Manager", "Community Manager", "Guest Experience Manager"). At
the same time it had no way to find a strong role that happened to be generically titled.

**What changed**

- Bare broad words removed from the function group. Their **meaningful compounds stay** —
  `customer experience`, `employee experience`, `member experience`, `customer success`,
  `service delivery`, `client services`, `people operations`, `AI enablement`, `AI adoption`.
  Those terms now only help when they carry real function context, which is exactly what the
  spec asked for.
- Priority and adjacent families added: PMO, program/portfolio management, operational
  excellence, organizational strategy/design/effectiveness, process improvement, value
  realization, workforce transformation, service design, content strategy, digital strategy,
  marketing operations, SEO, learning & development, M&A integration.
- **Exclusions grew from 12 to 67 terms**: accounting, FP&A, tax, treasury, audit,
  controller, payroll, AP/AR; software/data/security engineering, DevOps, SRE, sysadmin,
  help desk, technician; clinical, nursing, physician, patient care; plant, production
  floor, warehouse, depot, maintenance; account executive, sales development, SDR, BDR;
  intern, coordinator, admin/executive assistant.
- **Deliberately NOT excluded**, each for a stated reason:
  - `manufacturing` — transformation, integration and operational-excellence work *inside* a
    manufacturer is highly relevant. Only `manufacturing engineer` is out.
  - bare `finance` — so "Director, Finance Transformation" survives; the specific finance
    *functions* are excluded instead.
  - bare `development` — would have killed `leadership development` and
    `organizational development`.
  - `contract` / `temporary` / `interim` — contract leadership work is in scope.
- Manager, Senior Manager, Lead and Principal remain eligible in the seniority group.

**New engine capability — `mandate_rescue`** (opt-in per profile; Chad does not have it).
When a title misses the match groups, the role is still kept if it (a) reads as a leadership
role and (b) its **description** names at least 2 distinct *distinctive* mandate terms
(`target operating model`, `post-merger integration`, `organizational effectiveness`,
`value realization`, …). Exclusions are applied **first**, so a rescue can never resurrect an
accounting or engineering role.

This was tuned against live feeds, not guessed. A first attempt included generic manager-JD
boilerplate (`cross-functional`, `stakeholder management`, `program management`,
`continuous improvement`, `p&l`) and rescued obvious non-targets — "Lead, Benefits" and a
bare "Project Manager". Those terms were removed; a regression test now locks that out.

**Measured effect on live feeds:** Lisa local 19 → 11, US-remote 26 → 18 (noise removed),
contract 6 → 10 (generically-titled transformation contract roles recovered).

---

## 2. Scoring and recommendations (WS2)

The single blended `{fit, score, reason}` verdict is replaced by:

`qualification_fit`, `interest_fit`, `practical_fit`, `opportunity_score` (each 0–100),
`recommendation` (`apply_first` | `strong_fit` | `stretch` | `practical_contract` |
`not_recommended`), `reasons` (≤5), `concerns` (≤3), `relocation_required`,
`relocation_assistance_mentioned`, `signing_bonus_mentioned`.

**Validation (`validate_verdict`)** is strict where it matters and forgiving where it doesn't:

- **Rejected outright** if any of the four scores or the recommendation is missing or
  malformed — a wrong recommendation is worse than no recommendation. Booleans are rejected
  as scores so `true` cannot silently become `1`.
- **Coerced** otherwise: scores clamped to 0–100, floats rounded, `reasons`/`concerns`
  capped, a bare string accepted where a list belongs, and the three mention-booleans
  defaulted to `false` so a benefit is **never inferred**.
- Any rejection returns a neutral verdict with `score -1`, which is **never cached**, so the
  role is kept in the email and retried next run rather than frozen with a bad verdict.

**Location weighting** is instructed in the prompt: US-remote highest, Utah local/hybrid
high, hybrid outside Utah reduced, onsite outside Utah reduced for likely relocation — with
an explicit instruction that a weak `practical_fit` must not erase a strong role.

**No automatic penalty** for IC / Lead / Principal / Manager / Senior Manager / contract /
temporary roles, stated in both the prompt and `lisa_background.json`.

**Cache safety.** `FIT_SCHEMA_VERSION` is folded into the cache fingerprint, so no verdict
written under the old schema can be read by the new code. This forces **one full re-score**
of the ~136 tracked roles — roughly **$1–2** at current Sonnet pricing, one time.

**Token use.** `lisa_background.json` was rewritten as a lean scoring profile (8.0 KB → 5.7 KB).
Honest accounting: the richer rubric ate that saving, so per-call size was initially a wash.
The actual reduction comes from **prompt caching** — the rubric + candidate profile are
byte-identical for every call in a run, so they move into a cached `system` block (~2,186
tokens, comfortably over the 1,024-token minimum), leaving ~550 varying tokens per posting.
A cache miss costs exactly what the previous version cost, so this is a saving, not a
dependency. Per-run token and cache-hit figures are printed after each run.

---

## 3. Lisa's email (WS3)

Lisa's email is now a **ranked daily digest** (`build_digest_html`). Chad keeps the original
lane-by-lane renderer — he has no `report_style` key.

- **The duplicate is gone.** No more "What's changed" *and* "All current matching roles".
  Verified: zero duplicate job links in the rendered email.
- **A digest, not a change log**, per Lisa's decision: it shows the best currently-open roles
  whether they first appeared today or last week, because people rarely apply on day one. A
  role recurs across days while it stays open — but appears **exactly once per email**.
  Repetition is controlled by feedback, not by hiding things.
- **Sections in spec order**, each role in exactly one, by precedence: contract lane →
  Contract; strong recommendations → Top (overflow into Additional); remaining Utah-local →
  Utah; everything else → Additional.
- **Per-section quotas (6 / 4 / 3 / 2), not one global cap.** A single global cap was built
  first and starved whole sections — 18 `apply_first` roles filled it and the Contract
  section rendered empty even though the staffing lane had matches.
- Labels: 🔥 Apply First · ⭐ Strong Fit · 🤔 Stretch · 💵 Practical Contract.
- Location: ✓ Remote · 📍 Utah · 📍 Hybrid (Utah) · 🏡 Hybrid (outside Utah) ·
  🏡 Relocation Required.
- Each card carries the label, title, company, location indicator, urgency band, salary when
  known, the opportunity score plus the three dimensions, up to five "Why this appeared"
  bullets, up to three concerns, and a direct link.
- **Urgency is separate from fit**, with an honest precision caveat: `posted` is a date with
  no time of day, and Workday's is reverse-engineered from "Posted 3 Days Ago". The freshest
  band therefore reads **"Posted today"** rather than claiming a 24-hour window.
- `not_recommended` roles are hidden from every opportunity section but **retained in the
  snapshot** for the weekly audit — which is why `fit_mode` stays `rank`, not `filter`.
- **Unscored roles are still shown**, so an API failure cannot silently hide work.
- **Lisa's window is 14 days; Chad stays at 7.** The shared fetch widens to cover the largest
  window, so this costs no additional API calls and does not change Chad's report.

---

## 4. Removed jobs and Hiring Progress (WS4)

**A real bug was fixed first.** When a company's ATS fetch raised, that company contributed
zero postings, so **every role known at that company read as "removed"** and was reported as
filled. `collect_pool` now returns the set of failed companies; those roles are held out of
the diff **and carried forward in the snapshot**, so they neither report as gone today nor
return as brand-new tomorrow. This fired on a live transient failure during development:
3 roles that would have been announced to Chad as filled were correctly held.

`classify_removal` then distinguishes what is genuinely knowable:

| Cause | Handling |
|---|---|
| Source error | Held upstream; never enters the removal list. Reported as "status unknown, not treated as closed". |
| Aged out of the window | Not reported as a departure — "may still be open". |
| Filtered by a rule change | "No longer matches the current search rules" — our change, not the posting's. |
| No longer present at source | "Posting no longer detected." |

Wording is cautious by construction. **Nothing is ever described as "filled"** — there is a
test asserting the word never appears in a rendered report. With feedback, an applied role
reads `Applied ✓ … Posting no longer listed`; otherwise `Not applied — Posting no longer
detected`.

A previously *rescued* role cannot be re-checked against the title gate (snapshots carry no
description), so a persisted `rescued` flag stops it being mislabeled as a rule change.

---

## 5. Feedback and the weekly audit (WS5)

**`feedback_lisa.json`** — one committed, hand-edited file; no UI. A role is identified either
by its exact `key` or, more easily, by `company` + `title`. Behavior matches Lisa's spec:

| Status | Effect |
|---|---|
| `applied` | Never recommended again; appears under Hiring Progress once the posting disappears |
| `already_applied` | Suppressed from recommendations |
| `not_interested` | Suppressed permanently |
| `too_technical` / `wrong_function` / `wrong_industry` | Suppressed, and counted as a false positive in the audit |
| `interested` | Keeps showing until it closes or ages out; badged ★ Interested |

A missing or malformed file is never fatal. **`feedback_template_<name>.json`** is regenerated
each run with every in-play role pre-filled, so giving feedback is copy-paste rather than
typing keys by hand. Nothing automatic changes scoring policy — the audit reports, a human
decides.

**The weekly audit makes no network calls.** This was a deliberate design decision. The first
implementation was a standalone script that re-fetched all ~112 companies weekly; that was
rejected because it would have roughly doubled our outbound traffic against third-party ATS
endpoints, violating the project's own "one run per day, don't hammer ATS endpoints" rule and
putting the *daily* run at rate-limit risk to serve the lowest-priority feature in the spec.

Instead the **daily run records what it already computes**:

- `rejects_<name>.json` — leadership-shaped roles that were fetched but filtered out, each
  with the rule that dropped it (last 10 days, capped per lane).
- `source_health.json` — per-company roles returned, fetch failures, and a
  `consecutive_zero_runs` streak, which is the ATS-migration / dead-slug signal.

`audit.py` reads only those files plus the snapshots and feedback, and writes `AUDIT_<name>.md`.
A test asserts `audit.py` contains no reference to `collect_pool`, `urllib`, or the API. Side
benefit: a weekly audit now sees a **full week** of accumulated data instead of one Monday
morning's fetch.

The audit immediately proved useful on real data, surfacing "GitLab — Director, Pipeline
Excellence" and "Enablement Content Manager" as near-misses dropped on the function group —
both arguably in Lisa's domain and worth a human decision.

---

## 6. Sources evaluated (WS6)

**None of the five were added.** Each was probed live; the reasons are specific, not
generic. The spec permits documenting why a source was not added, and explicitly warns
against letting source expansion flood the email with duplicate or low-quality roles.

| Source | Structured feed? | Verdict and reason |
|---|---|---|
| **Himalayas** | **Yes** — documented JSON API (`/jobs/api`) and RSS | **Not added.** Three disqualifiers: (1) `companyName` is the literal string `"name"` for **all 20** records on the page — company identity is only recoverable from a slug, which breaks both `key` identity and dedup against our ATS registries; (2) **every** `applicationLink` points to `himalayas.app`, never the employer's own posting, so there is no canonical company application page; (3) `totalCount` is **99,092** jobs at 20 per page — a full sweep is ~5,000 requests, incompatible with being a polite client. |
| **NoDesk** | **Yes** — RSS at `/remote-jobs/index.xml` | **Not added.** The feed carries only **10 items** (latest-only, no pagination), every link points to a `nodesk.co` landing page rather than the employer's posting, and the sampled content was general remote admin work ("Virtual Administrative Assistant"), not leadership roles. Negligible signal for a director-level search. |
| **Flexa** | No | **Not added.** `/feed` returns HTML, not a feed; no JSON API or RSS found. Also UK-centric, which fights the US-remote gate. |
| **Built In** | No | **Not added.** No RSS, no public API (`/jobs.rss`, `/api/jobs`, `/feed` all 404). Would require heavy HTML scraping of a JS app, which the constraints rule out. |
| **Remote100K** | No | **Not added.** No feed or API endpoint responded. |

**The deeper structural reason**, beyond any single source: the diff identity is
`"<Company>::<ats_id>"`, which `CLAUDE.md` correctly warns must never change. An aggregator
supplies its own IDs, so the same job arriving from an aggregator *and* from the company's own
Greenhouse board becomes two different keys — two cards, two scoring calls, two chances to
annoy. Deduplication would need a normalization layer keyed on company + normalized title,
and Himalayas' broken `companyName` makes exactly that key unreliable. Since both feed-bearing
candidates also fail the canonical-apply-link requirement, adding them would have cost
quality rather than added coverage.

**If source expansion is revisited**, the shape that fits this codebase is the existing one:
verified employers on tier-1 ATSes added to `remote_companies.json`, where the apply link is
canonical and the requisition ID is stable.

---

## Files changed

| File | Purpose |
|---|---|
| `jobmonitor.py` | Engine. Safe-run flags, source-error hold, `mandate_rescue`, multi-dimensional scoring + validation, prompt caching, digest renderer, removal classification, feedback, audit-trail writers, per-profile age windows |
| `profiles.json` | Lisa rebuilt (groups, 67 exclusions, `mandate_rescue`, `report_style: digest`, 14-day window). **Chad byte-identical to pre-V2** |
| `lisa_background.json` | Rewritten as a lean scoring profile reflecting the new criteria |
| `feedback_lisa.json` | **New.** Hand-edited feedback; starts empty |
| `audit.py` | **New.** Weekly audit; reads committed files only, zero network calls |
| `test_jobmonitor.py` | **New.** 141 offline tests, standard-library `unittest` |
| `requirements.txt` | **New.** Declares the optional `anthropic` dependency |
| `.github/workflows/prospector.yml` | Installs from `requirements.txt`; commits the new diagnostic artifacts |
| `.github/workflows/audit.yml` | **New.** Weekly audit, Mondays 15:00 UTC |
| `PROSPECTOR_DISCOVERY.md` | Current-state documentation of V1 (written before the changes) |
| `PROSPECTOR_V2_CHANGELOG.md` | This file |
| `PROSPECTOR_TESTING.md` | **New.** Non-developer instructions |
| `CLAUDE.md` | Updated for the V2 architecture |

Generated per run (committed by CI): `snapshot_*.json` (state), `report_*.md/html`,
`rejects_*.json`, `source_health.json`, `feedback_template_*.json`, `AUDIT_*.md`.

---

## Behavior changes to expect on the first production run

1. **One full re-score** (~136 roles, ~$1–2) because the verdict schema changed. Subsequent
   days cost only new postings, as before.
2. **Lisa's first digest will show a large "no longer match the current search rules" count.**
   That is the profile rewrite, not roles closing — and it is labeled as such rather than
   being called "filled". It settles after one run.
3. Lisa's role counts drop (noise removed) while her contract lane grows slightly.
4. Chad's email should look exactly as it did.

---

## Unresolved limitations

1. **The live API path is unverified.** No `ANTHROPIC_API_KEY`, `anthropic` SDK, or `ant` CLI
   was available in the development environment, so the real model's replies have never been
   round-tripped through `validate_verdict`. The logic is covered by 141 offline tests against
   a fake client, and failure degrades to *unscored but still visible* roles rather than a
   broken report — but the first CI run is the real test. **Watch that first email.**
2. **`anthropic` is unpinned.** Pinning to a guessed version risked breaking CI, and the
   resolved version could not be verified offline. Pin it in `requirements.txt` after the
   first successful run (see the comment in that file).
3. **Mandate rescue cannot fire for 11 local companies.** SmartRecruiters and Workday return
   no description at list time (Adobe, Ancestry, Health Catalyst, Cricut, Vivint,
   Instructure, Domo, Pluralsight, WGU, O.C. Tanner, BYU), so a generically-titled role at
   those employers stays invisible. Fixing it needs a per-posting detail fetch, which
   conflicts with the polite-client rule.
4. **`relocation_assistance_mentioned` / `signing_bonus_mentioned` are description-dependent.**
   For those same 11 title-only companies they will always be `false` — correct behavior
   under "never infer", but it means absence is not evidence of absence.
5. **Hybrid detection is heuristic.** It looks for "hybrid" / "days in office" / "days onsite"
   in the title or description. A posting that requires hybrid work without saying so is
   labeled by its location only.
6. **"Posted today" is the honest floor.** There is no sub-day precision available; Workday's
   dates are approximated from relative text.
7. **Visible-count target.** Quotas cap the digest at 15 (ceiling 18). Whether a typical day
   lands in the 8–12 target depends on how generously the live model scores; that could not be
   measured without API access. If every email arrives at 15, lower `DIGEST_QUOTAS` in
   `jobmonitor.py` — or ask for it and it can be made a setting.
8. **`rejects_*.json` and `source_health.json` grow slowly.** Rejects are capped at 10 days ×
   60 per lane; source health is one row per configured company per run. Both are diagnostic
   and safe to delete.
9. **The audit's near-miss list is only as fresh as the last daily runs** — the deliberate
   trade for making it network-free. Right after editing a rule you must wait for the next
   daily run to see the effect there (or run `--dry-run` locally, which is immediate).

---

## Rollback

```bash
git checkout v1-pre-v2 -- .        # restore every file to the pre-V2 state
git commit -m "Roll back Prospector V2"
git push
```

Snapshots are state, so also consider whether to keep the newer ones. See
`PROSPECTOR_TESTING.md` → *Rolling back* for the safe order and the snapshot caveat.
