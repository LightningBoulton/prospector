# CLAUDE.md — Prospector

Daily job-posting monitor for tech companies within ~30 miles of South Jordan, UT
(Silicon Slopes). Fetches each company's open roles from its ATS, filters through
named per-person "profiles," diffs against yesterday, and emails a report per person.
Full narrative + GitHub Actions setup in @README.md.

## Commands

- `python3 jobmonitor.py` — fetch all companies once, run every enabled profile.
- `python3 jobmonitor.py --profile <name>` — run one profile.
- `python3 jobmonitor.py --list` — list profiles.

Python 3.12, **standard library only — do not add dependencies** unless a tier-3
(Playwright) source genuinely requires it. No build step, no tests framework yet.

## Architecture

Pipeline, per run: read `companies.json` → fetch + normalize every source **once** →
apply global location gate → for each profile: title-filter → diff vs the profile's
snapshot → write new snapshot + report. Fetch is shared across profiles; keep it that
way (one API call set per run, not per profile).

Tiered fetch strategy: (1) ATS JSON API [current], (2) HTTP + HTML parse, (3) Playwright.
Stay in tier 1 whenever possible — it's why this is low-maintenance.

## Files

- `companies.json` — `companies[]` + `needs_identification[]` backlog. Company fields: {name, city, ats, slug}; **workday entries also need** {wd_host, site}.
- `profiles.json` — `profiles[]` ({name, label, enabled, match_groups, exclude_any}).
- `settings.json` — run-wide tweakables (loaded by `load_settings`, defaults in `SETTINGS_DEFAULTS`): `max_posting_age_days` (drop postings older than this; 0/null = keep all; unknown-date always kept), `fit_scoring_enabled` (master off-switch for the Anthropic API), and `star_within_days` (⭐ postings newer than this in the report; 0/null off — `main` sets the `STAR_WITHIN_DAYS` global from it). Missing file/keys fall back to defaults.
- `jobmonitor.py` — the engine. Key functions: `fetch_greenhouse/lever/smartrecruiters/workday`, `collect_pool`, `matches_profile`, `enrich_salary`, `enrich_with_fit`, `diff`, `build_report`, `build_html_report`, `run_profile`.
- `snapshot_<name>.json`, `report_<name>.md`, `report_<name>.html` — generated per profile; committed by CI.

## Data model

Every posting normalizes to:
`{key, company, title, location, url, posted, salary, description}` (+ private `_ats`, `_detail_url`)
`key = "<Company>::<ats_id>"` is the **stable diff identity** — never change how it's built,
or every existing snapshot will read as fully new/removed on the next run.
`posted` = best "first posted" date (YYYY-MM-DD) from the list feed; rendered as
"Posted Jul 10 · 3d ago" (`_fmt_posted`). `salary` = pay string when known. `description`
feeds LLM scoring + salary regex. `description`, `_ats`, and `_detail_url` are **stripped
before the snapshot is written** (`run_profile`). Diff ignores everything but `key`/`title`.

Diff: compares `key` sets. `new` = in current not previous; `removed` = in previous not
current; `changed` = same `key`, different `title`.

## Posting date & salary enrichment

- **`posted`** is free from every list feed (Greenhouse `first_published`, Lever `createdAt`,
  SmartRecruiters `releasedDate`, Workday `_workday_date(postedOn)` — approximate).
- **`salary`**: Lever gives a structured `salaryRange` inline (`_lever_salary`). For the others,
  `enrich_salary` regexes a "$X–$Y" range (`_PAY_RE`) out of the `description` already fetched
  (Greenhouse), and only falls back to a per-posting detail fetch (`_detail_text`) when there's
  no description (SmartRecruiters/Workday are title-only). Cached by `key` (`_SALARY_CACHE`) so a
  role matched by multiple profiles costs one fetch. Keep it bounded to matched roles — the only
  place besides the list feeds the engine makes calls. Salary is best-effort: absent when a
  posting doesn't state pay.

## ATS integration notes (hard-won — read before touching fetchers)

- **Greenhouse**: `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` → `{jobs:[{id,title,location:{name},absolute_url,first_published,updated_at,content}]}`. Use `first_published` for `posted` (fallback `updated_at`); `content=true` gives the HTML description (LLM + salary regex). 404s on bad slug. A real board can legitimately return `jobs:[]`.
- **Lever**: `GET api.lever.co/v0/postings/{slug}?mode=json` → `[{id,text,categories:{location},hostedUrl,createdAt,salaryRange,salaryDescriptionPlain,descriptionPlain}]`. `text` is the title. `createdAt` is **epoch milliseconds**. **`salaryRange {min,max,currency,interval}` is inline** — the only ATS here giving structured pay free. 404s on bad slug.
- **SmartRecruiters**: `GET api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N` → `{totalFound,content:[{id,name,location:{city,region,remote,fullLocation},releasedDate}]}`. `name` is the title; `releasedDate` → `posted`. **Returns HTTP 200 with `totalFound:0` for ANY slug, including nonsense** — so a 0-count SmartRecruiters response does NOT confirm the company exists. Always verify count > 0 when adding one. Paginate via `offset` until `offset >= totalFound`. Public apply URL: `https://jobs.smartrecruiters.com/{slug}/{id}`. No description in the list; salary regexed from the detail endpoint `jobAd.sections` (`/postings/{id}`).
- **Detail endpoints (salary fallback, matched roles only)**: Greenhouse `content`, SmartRecruiters `jobAd.sections`, Workday `jobPostingInfo.jobDescription` — HTML, cleaned by `_clean_html` and regexed by `enrich_salary`.

## Profile matching semantics

In `matches_profile`: title lowercased; keep iff it matches **none** of `exclude_any`
AND matches **at least one term in every** `match_groups` entry (AND across groups,
OR within). Matching uses `\bterm\b` regex — **word-boundary aware on purpose** so short
tokens (`coo`, `vp`, `cco`) don't match inside longer words (`coordinator`, `account`).
Preserve the word-boundary behavior. Empty `match_groups` = keep all local roles.

## Location + age gates

Global, applied once to the pool in `collect_pool` before profiles. Location:
`LOCAL_KEYWORDS`, `KEEP_REMOTE`, `LOCAL_ONLY` (constants atop `jobmonitor.py`). Age:
`max_posting_age_days` from `settings.json` (`_within_age`) — drops confidently-too-old
postings, saving downstream salary/LLM calls. If a future profile needs its own geography,
add a per-profile override rather than widening the global list. Note: a role that ages out
of the window leaves the pool and thus shows up as **removed** in that profile's next diff.

## Adding tier-2 companies (the main backlog)

Big local employers (eBay, Ancestry, BambooHR, Health Catalyst, Ivanti) are on
Workday/iCIMS/custom, not the three JSON APIs. Preferred path:

- **Workday** (most common here): `fetch_workday` is IMPLEMENTED and registered. `POST https://{wd_host}/wday/cxs/{slug}/{site}/jobs` with `{"appliedFacets":{},"limit":20,"offset":<n>,"searchText":<optional>}` → `{total, jobPostings:[{title, externalPath, locationsText, postedOn, bulletFields}]}`. Stable id = `bulletFields[0]` (req number). `postedOn` is relative text → `_workday_date()`. Apply URL = `https://{wd_host}/{site}{externalPath}`. Config: {ats:"workday", slug:<tenant>, wd_host, site}; **find the real careers URL — do NOT guess wd_host/site, most guesses 404**. Verified working: Pluralsight, Domo, Adobe.
  - **GOTCHA (cost us a bug):** Workday reports the real `total` ONLY on the first page (offset 0); later pages return `total:0`. Capture total once from page 0 — re-reading it per page stops the loop early on 3+ page tenants.
  - **Large/global tenants** (Adobe ~897, eBay): set optional `"search_text":"Lehi"` in config to scope server-side (897→~65) instead of paging all. Multi-location hits come back as "N Locations"; `fetch_workday` relabels those to "Lehi (+N more)" so the local gate keeps them.
- **Ashby**: `GET api.ashbyhq.com/posting-api/job-board/{slug}` → `{jobs:[...]}`; 404s on bad slug.
- Only reach for Playwright if a company has no reachable JSON at all.

Each new ATS = one `fetch_*` function returning normalized postings + one `FETCHERS` entry.

## LLM fit scoring (optional relevance filter)

Ranks each profile's matched roles by how well they fit a candidate, via the Anthropic API.
- **Activation:** on only when `settings.json` `fit_scoring_enabled` is true (master off-switch — set false to skip all API calls for test runs) AND `ANTHROPIC_API_KEY` is set (env) AND the profile has a `background_file`. Otherwise the whole feature no-ops and the tool behaves exactly as before. `anthropic` SDK must be installed (`pip install anthropic`) — CI needs this in the workflow.
- **Config:** profile fields `background_file` (path to a candidate JSON, e.g. `lisa_background.json`) and `fit_mode` = `"rank"` (keep all, sort by score — default/safe) or `"filter"` (drop model-rated `"no"`).
- **Flow:** `enrich_with_fit` runs after keyword match, before diff. It **only scores NEW roles** — verdicts are cached in the snapshot's `fit_result` and reused, so daily cost ∝ new postings, not total. `score_fit` returns `{fit: yes|maybe|no, score: 0-100, reason}`; on ANY API/parse failure it returns a neutral `maybe`/score -1 (role kept, run never crashes) — and **failures aren't cached** (`score < 0` is never reused), so a transient parse error retries next run.
- **Cache invalidation:** each verdict stores a `bg` fingerprint (`_bg_fingerprint` = short hash of the background content). A cached verdict is reused only if its `bg` matches the current background. **Editing a `background_file` auto-invalidates that profile's verdicts** → full re-score next run, no manual clearing. Legacy verdicts with no `bg` also re-score once.
- **Descriptions:** `fetch_greenhouse` (`?content=true`, HTML-cleaned) and `fetch_lever` (`descriptionPlain`) now include a `description` field for better judgments. SmartRecruiters/Workday are title-only (no cheap bulk description). `description` is stripped before the snapshot is written (kept lean); `fit_result` is persisted.
- **Model:** `FIT_MODEL` constant (currently `claude-sonnet-5`; `claude-haiku-4-5-20251001` for lower cost). Verify current model strings against docs.claude.com.

## Conventions & guardrails

- IMPORTANT: **no secrets in the repo.** Email creds/recipients + `ANTHROPIC_API_KEY` are GitHub Actions secrets (`MAIL_USERNAME`, `MAIL_PASSWORD` = Gmail App Password, `MAIL_TO_*`, `ANTHROPIC_API_KEY`). Never hardcode an email address (Lisa's included) or key in committed files.
- Be a polite client: one run/day; don't parallel-hammer ATS endpoints.
- Snapshots are state — CI commits them back, and they carry the `fit_result` cache (each stamped with a `bg` fingerprint). Don't rename/relocate or wipe them casually (wiping forces a full re-score = full API bill). To deliberately force a re-score, edit the `background_file` (auto-invalidates) rather than deleting the snapshot.
- Core fetch/diff stays dependency-free; `anthropic` is the only optional dependency and must stay optional.

## Next tasks (priority order)

1. ~~Relevance scoring~~ DONE (LLM fit scoring above). Tune `lisa_background.json` and prompt; consider `fit_mode:"filter"` once verdicts are trusted.
2. Tier-2/50-mi coverage for the `needs_identification` list — biggest payoff for both reports.
3. Source-health alert when a known company's endpoint drops to zero (likely ATS migration).
4. Optional HTML email bodies (nice with the ranked/scored layout).
