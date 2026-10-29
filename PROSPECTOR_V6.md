# Prospector V6 — JD-first judgment, fewer and better recommendations

Lisa's 2026-10 update, implemented. It **supersedes conflicting earlier search, matching,
scoring, candidate-context and output rules for Lisa's profile only.** Chad's profile opts into
none of it: his rubric, his cache fingerprint and his email are unchanged.

The objective changed from "find every plausible job" to **"identify the few she would
realistically want, is credibly qualified or near-qualified for, and that are worth limited
application time."** Retrieval stays broad, and the final selection is now much stricter.

V6 is built on V5, which was merged from `fix-source-health-signal` in the same change. V5 had
never reached `main` before this.

---

## What changed, and where

| Lisa's section | Implemented in |
|---|---|
| 1-3, 16-19, 22-23, 29: JD-first, dominant work, A/B/C/D, false positives/negatives, accuracy | `jobmonitor._fit_instructions_jd_first` (profile `fit_rubric: "jd_first"`) |
| 4: gates vs preferences vs cautions | the rubric + `screening._is_gate` + `enforce_hard_requirements` (profile `hard_requirements.gate_categories`) |
| 5: CONFIRMED / TRANSFERABLE / UNKNOWN / GAP | verdict `key_qualifications` |
| 6-11, 13-14, 20-21: canonical candidate context | `lisa_background.json` (rewritten) |
| 12: industry relationship separate from importance | verdict `industry_relationship` + `industry_importance` |
| 15: sales-adjacency lane | `jobmonitor.sales_adjacency_match` (profile `sales_adjacency`) + verdict `sales_readiness` |
| 24: exceptional-opportunity override | verdict `exception_reason`; one slot per run |
| 25-26: at most 5, zero allowed, ranked by expected value | `change_sections` → `_selection_sections` (profile `selection`) |
| 27-28: concise decision card, no user busywork | `_decision_card` / `_decision_lines`; rubric bans research/verify actions |
| 31: implementation test | `check_v6.py` (live) + `TestV6*` in `test_jobmonitor.py` (offline) |

### The verdict (jd_first rubric)
All existing fields are kept, because they drive ranking and every V5 guarantee. The new
fields are `dominant_work`, `dominant_share`, `work_interest` (A-D), `fit_level`,
`industry_relationship`, `industry_importance`, `key_qualifications`, `cautions`,
`sales_readiness`, `action_status`, `why_fits`, `material_gap`, `next_action` and
`exception_reason`. If any of them comes back malformed it **degrades** to a default; it
never rejects the verdict. **`unmet_requirements` now means TRUE GATES only.** That is what
caps a role, so a preference must never land in it.

`JD_FIRST_RUBRIC_VERSION` is folded into Lisa's cache fingerprint. Bump it whenever that
prompt or its fields change meaning. `FIT_SCHEMA_VERSION` was deliberately **not** bumped,
because that would have re-scored Chad for a change he is not part of.

### Gates vs cautions (deterministic half)
`hard_requirements.gate_categories` = license/certification, mandatory security clearance,
engineering/architecture depth. A detected requirement in those categories caps the role at
`stretch`, but only when the JD words it as mandatory: a strong cue ("required", "must have",
"mandatory") nearby, and no softener ("preferred", "a plus"). **Every other category (specialist
tenure, industry specialization, SQL, CRM admin, RevOps, renewals, recruiting, manufacturing)
is now a CAUTION.** It shows on the card and never caps. Whether such a requirement is a true
gate for a particular job ("genuinely mandatory years … central to the job") is the model's
call, and the model's `unmet_requirements` still caps. **Assumption:** the deterministic
layer cannot judge centrality, so it only acts on the requirements that are unambiguous.

`enforce_hard_requirements` is now **opt-in** (a `hard_requirements` or `technical_exclusion`
block). Under V5 it ran for every profile, so it was capping Chad's engineering roles for
"requires Python".

### Sales-adjacency lane
This is an **evaluation** lane, not a new registry: it runs over every source that is already
fetched. It retrieves consultative technical-sales titles (Solutions/Value Consultant, Value
Engineer, Pre-Sales, Solution/Customer/Experience Strategist, Strategic Customer Success,
Sales Strategy, Sales/Solutions Engineer). It runs before `discovery.exclude_any`, so
"Value Engineer" is not dropped for containing "engineer". It has its own exclusions
(SDR/BDR, inside sales, AE, cold calling, outbound, lead generation). The **technical
description screen still applies**, so a sales engineer whose JD asks for coding is dropped.
A title the primary core gate already claims ("Director, Value Realization") stays primary.
Sales-lane roles get deep JD retrieval.

### Selection
A record is worthwhile only when it has a jd_first verdict **and** it is apply_first or
strong_fit (or practical_contract for the contract section), with A/B/C dominant work.
Sales-lane roles also need `qualified_now` or `near_qualified_december`. A stretch, or D-tier
work, gets in only with a named `exception_reason`, and gets at most one slot.

**At most 5 new per run.** The primary search claims slots first; contract (≤2), sales (≤2)
and exception (≤1) only fill what it leaves. **Assumption:** that is the literal reading of
"must not crowd out the primary search" together with "max 5 per run". Worthwhile roles cut
by the cap are kept and can appear on a later day. **Zero is a valid result**, and the email
says so. Still-open, previously-emailed worthwhile roles stay visible (≤5), and the hero names
the **best move across new AND still-open roles**. A record without a jd_first verdict is
never shown on an old verdict; it is counted as "awaiting a JD review".

The section quotas and the Wildcards / "New — worth reviewing" sections do not apply in
selection mode (V3's "2-4 wildcards" is superseded by "do not manufacture"). QA check 7
(SEO coverage) now promotes only a worthwhile role, and only into a free slot.

### Card
Card format: Company — Role · Status (APPLY / VALIDATE / CONTACT / PACKAGE / STRETCH) · Work
Interest · Fit (Strong / Good / Stretch) · Sales readiness (sales lane) · Industry ·
Compensation · work model/location · Why it fits · Material gap · Next action. **No numeric
score is shown.** The score still ranks internally. A role capped by a gate always reads
STRETCH.

---

## Limitations (stated plainly)

- **Compensation is never "searched".** Pay comes from the posting and the existing
  salary-enrichment regex. Prospector has no web search, so "not stated in the posting" means
  exactly that.
- **Retrieval geography did not widen.** The location gates are unchanged: US-remote + Utah
  everywhere, plus WA/CA/OR in the priority lane. "Western US / relocation" therefore affects
  *judgment* (practical_fit), not which postings are fetched. Widening it would change fetch
  volume and cost, and would need a REGION_GATES entry per state.
- **The candidate context is a committed file in a PUBLIC repository.** It now includes Lisa's
  compensation targets and her earlier career history.
- **First runs are budget-limited.** Every one of Lisa's verdicts re-scores (new rubric and
  new context), and `max_new_scored_per_run` is 150. On the dry-run pool that left ~177 roles
  deferred, so the full effect takes about 2-3 daily runs. Roles not yet re-scored are
  withheld, not shown on stale verdicts.
- **Adobe training status is static.** It reads `in_progress` until someone edits
  `lisa_background.json`.

## Operating

```bash
python3 test_jobmonitor.py                        # 401 offline tests
python3 check_v6.py --offline                     # tests E (context) + G, no API
ANTHROPIC_API_KEY=... python3 check_v6.py         # tests A-G against live postings (~11 calls)
python3 jobmonitor.py --dry-run --fake-fit --profile lisa   # preview the email layout free
```
