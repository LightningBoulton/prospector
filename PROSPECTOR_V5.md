# Prospector V5 — stricter recommendations, honest status, reliable delivery

Nine targeted changes to the existing engine, from the product owner's list. **Not a
rewrite.** Discovery breadth is preserved and deliberately widened; the tiered ATS fetchers,
the lane machinery, the fit-verdict cache, the lifecycle database and Chad's report are all
either untouched or extended in place.

The theme running through all nine: **broad discovery, much stricter final recommendations.**
Retrieval got wider (SEO/content, recruiting operations, California, Oregon). Everything
downstream of retrieval got harder to pass.

---

## The nine, and where each one lives

| # | Requirement | Where it is implemented |
|---|---|---|
| 1 | Reliable 6:45 a.m. Mountain delivery | `delivery.py`, `.github/workflows/prospector.yml`, `run_log.json` |
| 2 | Technical-role exclusion, before scoring | `screening.technical_screen` → `jobmonitor.classify_match` |
| 3 | Hard requirements outrank similarity | `screening.hard_requirements` → `jobmonitor.enforce_hard_requirements`; `deepen_descriptions` |
| 4 | Recruiting judged on substance | `screening.recruiting_gaps`, the rubric, `bridge_skills` |
| 5 | LIVE VERIFIED / NEEDS RECHECK / CLOSED VERIFIED | `lifecycle.py` — `refresh_statuses`, `close_missing`, `verify_missing` |
| 6 | No recycled "new" roles | `lifecycle.resolve_key` + the alias index; `change_state` |
| 7 | Remote eligibility from the JD | `screening.remote_eligibility` → `jobmonitor.apply_remote_eligibility`; `is_california` / `is_oregon` |
| 8 | Discovery breadth + SEO/content | `profiles.json` `discovery.family_only_any`, `title_any`, `match_groups` |
| 9 | Final QA before sending | `qa.py` |

Three new modules, all standard library, all offline except one bounded verification call:
**`screening.py`** (deterministic JD analysis), **`qa.py`** (the pre-send gate), and
**`delivery.py`** (run timing and the delivery ledger).

---

## 1. Reliable morning delivery

**What was wrong.** The schedule was `cron: "0 13 * * *"` — a fixed UTC time, so 07:00
Mountain in summer and 06:00 in winter, and no recovery when GitHub started it late or the
run failed. Delivery had drifted from ~07:45–08:05 to 11:00–17:00.

**Three causes, three fixes.**

*DST.* GitHub cron is UTC-only. Both candidate times are now scheduled — `45 12` and
`45 13` — and a guard step runs the work only on the one that is genuinely 06:45 in
`America/Denver` today (`delivery.should_run_now`, via `zoneinfo`). No date in March or
November to remember.

The tolerance is **59 minutes, not 60**, and that minute is load bearing: the wrong-season
cron fires at exactly +1h, so a 60-minute window would let both fire and produce two runs a
day. At 59 the off-season cron is always rejected while a right-season cron that GitHub starts
up to 59 minutes late still produces the morning email.

*Late or failed runs.* A third cron at 16:30 UTC is a **catch-up**. It does the work only if
no run has SUCCEEDED today (`delivery.completed_today`), which it can tell because
`run_log.json` is committed state. The main run also **retries once** after 180 seconds:
almost every failure here is a transient network problem on the way to an ATS, and the run is
safely repeatable because state is written at the end and the fit-verdict cache means a retry
re-scores nothing.

*One canonical email.* Adding a catch-up creates the risk of two emails. `run_log.json`
carries a **delivery ledger**, and `<name>_email` — the output the workflow's mail step is
gated on — is now `email_enabled_for(...) AND NOT already_delivered_today`. The ledger is
written **only by `--confirm-sent`**, from the step that knows the mail actually went out, for
the same reason `ever_shown` is: rendering a report is not delivering it, so a run that
rendered and then failed to send must be free to try again.

`<name>_email` stays a separate output from `<name>_changed`. `force_email` ORs against
`_changed`, so folding them together would let a manual run defeat both a deliberate pause and
this gate. `TestWorkflowGating` and `TestDeliverySchedule` bind that.

**The log.** Every run records scheduled start, actual start, delay against the 06:45 target,
completion time and status; `--confirm-sent` adds the email-send time. `python3
jobmonitor.py --timing` prints today's.

---

## 2. The technical-role exclusion

Roles requiring the reader **personally** to have substantial expertise in software
engineering, architecture/infrastructure, APIs/integrations, data engineering, ML/AI
engineering, automation development or technical implementation are dropped **before
scoring**, so they cost nothing.

**It sits ABOVE the core title gate, and that placement is the requirement.** The list is
explicit that it applies "even when the title says Program Manager, Transformation,
Operations, AI, Customer Success" — which is only true if it can drop a role that satisfies
`match_groups`. Below the gate, exactly those titles would have sailed through. That is how a
*Senior Forward Deployed Architect* became a top recommendation.

**Read the omissions as carefully as the entries.** The term lists live in `screening.py`
next to the reasoning:

- **`integration` is NOT excluded, and must never be.** M&A integration, post-merger
  integration and the integration management office are among the reader's strongest domains.
  Only the engineering compounds — `integration engineer`, `integration developer` — are
  listed. Getting this wrong would delete her best category, so a test guards it.
- **`platform`, `automation`, `data` and `ai` are not excluded on their own.** "Platform
  Operations", "AI Adoption", "Automation Enablement" and "Data Governance" are business-side
  roles.
- **`technical` on its own is not excluded**, but `technical program manager` and `technical
  architect` are, because those two require engineering fluency to hold.

**Business-side AI is protected explicitly.** A JD about AI adoption that also talks about
model deployment is describing the technology being adopted, not the work. Such a role is
**kept, flagged `technical_risk`, and capped at `stretch`** by requirement 3's enforcement —
never dropped, never a top recommendation. That is what `business_override_any` does.

**Measured against the committed record set: 38 of 608 previously-retrieved roles are now
dropped on title alone** — 17 Technical Program Manager, 14 Architect, 3 Technical Project
Manager, and 4 others. No integration, platform or AI-adoption role among them.

**Chad has no `technical_exclusion` block, so he is not screened at all** and his engineering
search is byte-for-byte unchanged.

---

## 3. Hard requirements outrank semantic similarity

`enforce_hard_requirements` caps a role at **`stretch`** when the JD states a requirement the
reader does not support. Downgrade, not exclusion: the role stays visible with its gap named,
which is what lets the reader overrule us.

**Two independent detectors, either one sufficient.** The rubric asks the model for
`unmet_requirements`, and the model is usually right — but "usually" is not the guarantee the
requirement asks for. So `screening.hard_requirements` also runs: ten explicit categories
(specialist tenure, industry specialization, licenses, SQL/programming, CRM/MarTech admin,
engineering depth, dedicated RevOps, SaaS renewals, full-cycle recruiting, manufacturing),
word-boundary matched.

**The proximity test is what makes it usable.** A rule fires only when its token appears
within 140 characters of a requirement cue — "5 years", "must have", "required", "proven",
"expert in". Without it, every JD that mentions Salesforce in passing would downgrade and the
signal would mean nothing. `TestHardRequirements` binds both directions.

Generic overlap in strategy, transformation, operations, CX or leadership cannot buy a role
back up: the cap is applied after the model's judgment, not averaged into it.

**Enforcement runs at USE, not at scoring** — over cached verdicts too. A cached verdict was
produced by an older prompt against a thinner description; requirements 3 and 7 are guarantees
about what the reader is shown *today*.

### Deep JD retrieval, and why it is bounded

The requirement ends "only perform expensive/deep JD retrieval on roles that are otherwise
promising", and that constraint does real work. Three of the new rules can only be applied to
text, and the sources that give no text at list time — Workday, SmartRecruiters, Paradox — are
exactly where the reader's priority employers live. Without a description those employers
would be judged on a title.

So `deepen_descriptions` fetches the full JD, **before scoring** (so the model sees it too),
bounded by `settings.discovery.deep_retrieval_limit` (40 per lane), and spent only where
`screening.deep_retrieval_wanted` says the role already looks promising: a precise-gate match,
a named employer, or an SEO/content role. A failing detail endpoint costs depth, never the
run. **On the live pool this fetched 40 descriptions in the priority lane and 8 in the local
lane**, after which the technical screen re-runs and drops what the full text newly reveals.

---

## 4. Recruiting and talent: substance, not title history

Recruiting roles were never in the exclusion lists, but they were not being *found* either.
`recruiting operations`, `talent operations`, `talent strategy`, `talent analytics`,
`workforce planning`, `recruiting transformation`, `talent acquisition operations`,
`hiring manager enablement` and `recruiting process` are now in both `discovery.title_any` and
`match_groups`, so they can reach the core tier.

When such a role IS promising, the rubric requires `bridge_skills` — the **specific**
capabilities to bridge, not "recruiting experience" — and they are rendered on the card.
When the JD asks for direct execution (5+ years full-cycle, advanced sourcing, requisition
ownership, offer negotiation, hands-on ATS work), `screening.recruiting_gaps` names it and
requirement 3's cap applies.

`recruiting_gaps` returns nothing for a role that is not recruiting-shaped, so a
transformation role that happens to mention sourcing is not annotated as one.

**QA check 8** reports any recruiting role screened out as `not_recommended` with no stated
requirement or execution gap — that is the title-history judgment the requirement forbids, and
it is surfaced for a human rather than silently accepted.

---

## 5. Job status: three states, and only one of them may say "closed"

The old code wrote `STATUS_REMOVED` — rendered to the reader as "removed" — on nothing more
than a feed going quiet. `verification` is now a field **separate from** the lifecycle status,
because "how old and how repeatable is this?" and "do we actually know it is still open?" are
different questions that were being conflated:

- **`live_verified`** — an **authoritative** source listed it on this run.
- **`needs_recheck`** — it was live before and we cannot see it right now.
- **`closed_verified`** — the employer says so: a 404/410, an explicit "no longer available",
  or the posting's own stated deadline has passed.

**Source authority** (`AUTHORITY_EMPLOYER` > `AUTHORITY_SECONDARY` > `AUTHORITY_AGGREGATOR`,
derived from the existing `url_rank`): an employer/ATS board, or a staffing firm that is
itself the applier, listing a role IS that role being live. **An aggregator listing it is
not** — aggregators re-serve cached rows for days after a requisition closes — so an
aggregator-only sighting leaves the record in `needs_recheck`. When sources disagree, the
employer wins.

**`close_missing` no longer closes anything.** A missing record moves to `needs_recheck` and
records why (`not_returned` / `source_failed` / `aggregator_only`) and since when. A record
whose every source FAILED is additionally stamped `unverified_run` and its recheck clock does
not start — we did not look, we merely failed to look.

**`verify_missing` is the only route to `closed_verified`**, budgeted by
`lifecycle.recheck_limit` (20/run) and `recheck_after_days` (1 — a single morning's absence is
usually a feed hiccup). It prioritises roles the reader was actually shown, then score, then
how long they have been missing. `verify_open` stays conservative: a timeout or a bot wall is
UNKNOWN, and UNKNOWN never closes a role. If the employer still serves the page, the record
goes **back to `live_verified`** — the feed was wrong and the employer was right.

The "Removed since prior run" section now shows **only `closed_verified`** roles, and QA
check 4 enforces that at the last possible moment. Roles in `needs_recheck` are counted in a
one-line note instead: *"62 previously-emailed role(s) are not visible right now and are
marked NEEDS RECHECK — not reported as closed."*

---

## 6. No recycled "new" roles

**Identity.** `lifecycle.resolve_key` resolves a posting to an existing record through four
identities in order: the composite `company|normalized title|coarse location`, the **employer
requisition id** (the half of `key` after `::`), the **canonical employer URL** (lowercased,
tracking parameters stripped), and the source key. A persistent `aliases` index in
`jobs_<profile>.json` carries them. A database written before this change **rebuilds** what it
can from the source keys and URLs it already has, so the first run after the upgrade
recognises its own history instead of re-reporting all of it.

So a role does not become new because another source found it, a tracking parameter changed,
the title wording moved slightly, or it dropped out of a feed for a morning. Two genuinely
different city openings still stay two jobs.

**"NEW" now means genuinely never reported before** — `not ever_shown` — not "first seen
today", which is what it used to mean. Those differ constantly: a role first seen days ago
that never made the cut is still news to the reader, and one that was emailed last week is
not, however it re-entered the pipeline.

**Per-run change state**, rendered as a chip and used for ordering:

| state | meaning |
|---|---|
| `NEW` | never recorded before |
| `REVALIDATED` | was missing / needed a recheck, and is back |
| `REOPENED` | we **confirmed** it closed, and it is listed again |
| `UPDATED` | continuously open, but title, location, comp, URL or type moved |
| `STILL OPEN` | continuously open, unchanged |

`REOPENED` requires a *verified* closure. A legacy `STATUS_REMOVED` record — written by
pre-verification runs on a feed going quiet, never confirmed by anyone — returns as
`REVALIDATED`. Calling that a reopening would over-claim in exactly the way requirement 5
forbids; on the first upgraded run this was the difference between 13 false "REOPENED" chips
and 13 correct "REVALIDATED" ones.

Within "Still worth applying", changed states lead — a reopened or revalidated role is the one
the reader might otherwise think they had missed.

---

## 7. Remote eligibility comes from the JD

A location label is a label. "Remote — California" can mean a residence requirement, a pay
zone, an associated office, a preference, or genuine nationwide-US remote.
`screening.remote_eligibility` reads the JD for seven scopes, restrictions tested **before**
permissive phrases so a JD saying both "remote within the US" and "we can hire in CA, WA" is
`state_restricted`.

`apply_remote_eligibility` takes the **stricter** of the model's `remote_scope` and the text
detection. **`nationwide_us` is the only claim the report makes on the reader's behalf**, and
it needs both readings to agree. Silence in a JD is not permission to make it — a role with no
description lands on `unclear`, and the card says *"Remote — eligibility not stated"*.

**Preference order** is now `["remote_us", "utah", "washington", "california", "oregon"]`.
`is_california` and `is_oregon` join `REGION_GATES`, and both postal codes are ambiguous
enough to need care:

- **`CA` is also the country code for Canada.** "Toronto, CA" and "Vancouver, BC, CA" are how
  feeds write it. The bare code is accepted only when nothing else in the string looks
  Canadian (`_CANADA_CITIES` plus province codes matched in postal position). `ontario` and
  `vancouver` are deliberately NOT Canadian markers — Ontario is a California city and
  Vancouver is a Washington one.
- **`OR` is the English word "or".** A bare `\bor\b` would match "Sales or Marketing" and turn
  half the feed into Oregon. The code is matched only in postal position, `,\s*or\b`.

`is_local` is **unchanged**, so Chad's geography did not move.

---

## 8. Discovery breadth, with stronger SEO/content coverage

Everything previously searched is still searched. Added to `discovery.title_any` **and**
`match_groups[1]` (so they can reach the core tier): the SEO/organic-search/content families
and the recruiting-operations families.

**`discovery.family_only_any` is new**, and it is the literal implementation of "do not
require SEO/content roles to contain transformation language". The general discovery gate
wants a role family AND either seniority or mandate language in the description — but the
mandate vocabulary the description path matches on *is* transformation language. So SEO,
organic search and content-strategy families retrieve **on the title alone**.

This is the only place in the gate where one title term retrieves with no second signal, so
the list is short and specific by design; a test asserts that broad words (`content`,
`search`, `strategy`, `growth`, `digital`) never appear in it.

`technical seo` is deliberately **not** in the technical exclusion list — it is marketing
work, and the model judges it.

---

## 9. Final QA before sending

`qa.review` runs on the exact selection about to be sent, after every per-role rule, and
**corrects the report rather than reporting on it**.

Every check duplicates an upstream guarantee on purpose — if a cached verdict, a config edit
or a future change lets something slip, it is caught with the email still unsent. But three of
the eight can only exist here, because they are properties of the **selection**, not of any
role: what leads the email, whether a section contains repeats, and **what was left out** —
absence being invisible to a rule that only runs on what is present.

| check | on failure |
|---|---|
| Is the #1 role genuinely executable? | roles with an unmet requirement or technical flag leave Apply First for Wildcards |
| Did any hard requirement get ignored? | capped at `stretch` at send time |
| Are any "new" roles repeats? | previously-emailed roles move to "Still worth applying" |
| Are any "removed" roles only missing from a feed? | held back unless `closed_verified` |
| Did the employer source override aggregators? | warns on a multi-source role linking to an aggregator |
| Is remote eligibility supported by the JD? | withdraws the nationwide claim |
| Were strong SEO/content roles included? | **promotes** ones a section quota had cut |
| Were recruiting roles judged on duties? | warns on a screened-out role with no stated reason |

Downgrades, never deletions. Corrections are printed on every run and rendered into the email
under "Before sending, we checked", so a check that starts failing daily is visible.

The remote check reports a **correction only when it actually withdraws a claim** — a posting
that simply never stated its eligibility is the ordinary case, counted rather than itemised. A
daily "28 corrected" line for the normal state trains the reader to skip the whole block.

---

## Operating notes

```bash
python3 jobmonitor.py --dry-run --fake-fit   # full pipeline, no API cost, no email
python3 jobmonitor.py --check-schedule       # should this invocation do today's work?
python3 jobmonitor.py --timing               # today's scheduled/started/completed/emailed
python3 test_jobmonitor.py                   # 372 offline tests, no network, no API
```

**`FIT_SCHEMA_VERSION` is 4.** The verdict gained `unmet_requirements`, `bridge_skills` and
`remote_scope`, and the *meaning* of `apply_first`/`strong_fit` changed. Every stored verdict
re-scores once on the next real run. `lisa_background.json` also changed, which invalidates
her cache independently — both are intended.

**New state file: `run_log.json`.** It is committed, like the snapshots and the lifecycle DB,
because it is the timing record *and* the delivery ledger that keeps the daily email canonical
across runs on different machines.

**Nothing in Chad's path changed.** He has no `technical_exclusion` block, no
`family_only_any`, no `report_style`, and `is_local` is untouched. A regression test asserts
his engineering search still matches what it always did.
