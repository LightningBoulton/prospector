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
- `jobmonitor.py` — the engine. Key functions: `fetch_greenhouse/lever/smartrecruiters/workday`, `collect_pool`, `matches_profile`, `enrich_salary`, `diff`, `build_report`, `build_html_report`, `run_profile`.
- `snapshot_<name>.json`, `report_<name>.md`, `report_<name>.html` — generated per profile; committed by CI. The HTML is a dark-mode, email-safe (inline styles, table layout) version and is what the workflow emails via `html_body:`.

## Data model

Every posting normalizes to:
`{key, company, title, location, url, posted, salary}` (+ private `_ats`, `_detail_url`)
`key = "<Company>::<ats_id>"` is the **stable diff identity** — never change how it's built,
or every existing snapshot will read as fully new/removed on the next run.
`posted` = best "first posted" date (YYYY-MM-DD) each list endpoint gives. `salary` =
pay string if known. `_ats`/`_detail_url` are underscore-prefixed and **stripped before the
snapshot is written** (see `run_profile`). Diff ignores everything but `key` and `title`.

Diff: compares `key` sets. `new` = in current not previous; `removed` = in previous not
current; `changed` = same `key`, different `title`.

## Posting date & salary enrichment

- **`posted`** comes free from every list endpoint (Greenhouse `first_published`, Lever
  `createdAt`, SmartRecruiters `releasedDate`, Workday `_workday_date(postedOn)` — approximate).
  Reports show it as "Posted Jul 10 · 3d ago" via `_fmt_posted`.
- **`salary`**: Lever exposes a structured `salaryRange` in its list response (`_lever_salary`,
  free). The other three ATSes don't expose structured pay, so `enrich_salary` does a
  **per-matched-role detail fetch** and regexes a "$X–$Y" range out of the description
  (`_PAY_RE`, `_detail_text`). Enrichment runs once on the **union of matched roles across all
  profiles** (`main`), cached by `key` (`_SALARY_CACHE`) so a shared role costs one fetch — the
  only place the engine makes per-posting calls; keep it bounded to matched roles, never the pool.

## ATS integration notes (hard-won — read before touching fetchers)

- **Greenhouse**: `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs` → `{jobs:[{id,title,location:{name},absolute_url,first_published,updated_at}]}`. Use `first_published` for `posted` (falls back to `updated_at`). 404s on bad slug. A real board can legitimately return `jobs:[]`. No structured pay in the list; salary comes from regexing the `content` field on the per-job detail (`/jobs/{id}`).
- **Lever**: `GET api.lever.co/v0/postings/{slug}?mode=json` → `[{id,text,categories:{location},hostedUrl,createdAt,salaryRange,salaryDescriptionPlain}]`. `text` is the title. `createdAt` is **epoch milliseconds**. **`salaryRange` `{min,max,currency,interval}` is right in the list** — the only ATS here that gives structured pay for free (`_lever_salary`). 404s on bad slug.
- **SmartRecruiters**: `GET api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N` → `{totalFound,content:[{id,name,location:{city,region,remote,fullLocation},releasedDate}]}`. `name` is the title; `releasedDate` → `posted`. **Returns HTTP 200 with `totalFound:0` for ANY slug, including nonsense** — so a 0-count SmartRecruiters response does NOT confirm the company exists. Always verify count > 0 when adding one. Paginate via `offset` until `offset >= totalFound`. Public apply URL: `https://jobs.smartrecruiters.com/{slug}/{id}`. No structured pay; salary regexed from the detail endpoint's `jobAd.sections` (`/postings/{id}`).
- **Detail endpoints (salary only)**: Greenhouse `content`, SmartRecruiters `jobAd.sections`, Workday `jobPostingInfo.jobDescription` — all HTML, stripped and regexed by `enrich_salary`. Only hit for matched roles, never the whole pool.

## Profile matching semantics

In `matches_profile`: title lowercased; keep iff it matches **none** of `exclude_any`
AND matches **at least one term in every** `match_groups` entry (AND across groups,
OR within). Matching uses `\bterm\b` regex — **word-boundary aware on purpose** so short
tokens (`coo`, `vp`, `cco`) don't match inside longer words (`coordinator`, `account`).
Preserve the word-boundary behavior. Empty `match_groups` = keep all local roles.

## Location gate

Global, at top of `jobmonitor.py`: `LOCAL_KEYWORDS`, `KEEP_REMOTE`, `LOCAL_ONLY`.
Applied once to the pool before profiles. If a future profile needs its own geography,
add a per-profile override rather than widening the global list.

## Adding tier-2 companies (the main backlog)

Big local employers (Adobe, Domo, Pluralsight, eBay, Ancestry, MX, Podium, BambooHR)
are on Workday/iCIMS/custom, not the three current APIs. Preferred path:

- **Workday** (most common here): `fetch_workday` is IMPLEMENTED and registered. `POST https://{wd_host}/wday/cxs/{slug}/{site}/jobs` with `{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}` → `{total, jobPostings:[{title, externalPath, locationsText, postedOn, bulletFields}]}`. Stable id = `bulletFields[0]` (req number). `postedOn` is relative text → `_workday_date()` approximates it. Apply URL = `https://{wd_host}/{site}{externalPath}`. To add one: find the company's real careers URL (gives wd_host + site — do NOT guess, most guesses 404), add a config entry with {ats:"workday", slug:<tenant>, wd_host, site}. Verified working: Pluralsight (pluralsight.wd1.myworkdayjobs.com / Careers). Adobe is confirmed (adobe.wd5.myworkdayjobs.com / external_experienced) but returns ~895 global roles — before wiring it, add a location `appliedFacets` filter so it doesn't page through 45×20 results every run.
- **Ashby**: `GET api.ashbyhq.com/posting-api/job-board/{slug}` → `{jobs:[...]}`; 404s on bad slug.
- Only reach for Playwright if a company has no reachable JSON at all.

Each new ATS = one `fetch_*` function returning normalized postings + one `FETCHERS` entry.

## Conventions & guardrails

- IMPORTANT: **no secrets in the repo.** Email creds/recipients are GitHub Actions secrets
  (`MAIL_USERNAME`, `MAIL_PASSWORD` = Gmail App Password, `MAIL_TO_*`). Never hardcode an email address (Lisa's included) in committed files.
- Be a polite client: one run/day; don't parallel-hammer ATS endpoints. `enrich_salary` is
  the one place with per-posting calls — keep it bounded to matched roles and cached by `key`.
- Snapshots are state — CI commits them back. Don't rename or relocate them without a migration.
- Keep changes dependency-free and the list fetch shared-once (salary enrichment aside).

## Next tasks (priority order)

1. Relevance scoring on each profile's survivors (cheap keyword rank; optional LLM pass).
2. Tier-2 Workday coverage for the `needs_identification` list — biggest payoff for Lisa's report.
3. Source-health alert when a known company's endpoint drops to zero (likely ATS migration).
4. Optional HTML email bodies.
