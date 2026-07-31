# CLAUDE.md — Prospector

Daily job-posting monitor for tech companies within ~30 miles of South Jordan, UT
(Silicon Slopes). Fetches each company's open roles from its ATS, filters through
named per-person "profiles," diffs against yesterday, and emails a report per person.
Full narrative + GitHub Actions setup in @README.md.

## Commands

- `python3 jobmonitor.py` — fetch all companies once, run every enabled profile (writes PRODUCTION state).
- `python3 jobmonitor.py --profile <name>` — run one profile.
- `python3 jobmonitor.py --list` — list profiles.
- `python3 jobmonitor.py --dry-run` — **safe run**: snapshots + reports go to `.dryrun/`
  (seeded from a copy of production), and `GITHUB_OUTPUT` is never written, so no email can
  fire. Use this for every local test. `--snapshot-dir`/`--out-dir` redirect individually.
- `python3 jobmonitor.py --no-fit` — skip all Anthropic calls. `--fake-fit` — deterministic
  local fake verdicts for previewing email layout free (reports get a warning banner).
- `python3 test_jobmonitor.py` — 141 offline tests (stdlib `unittest`; no network, no API).
- `python3 audit.py` — weekly self-audit. **Reads committed files only; makes no network
  calls of any kind.**

Python 3.12, **standard library only — do not add dependencies** unless a tier-3
(Playwright) source genuinely requires it (`anthropic` is the one optional dep, declared in
`requirements.txt`, and must stay optional). No build step. Tests: `test_jobmonitor.py`,
stdlib `unittest`, offline — **run it after any engine change**.

## Architecture

Pipeline, per run: read `companies.json` → fetch + normalize every source **once** →
apply global location gate → for each profile: title-filter → diff vs the profile's
snapshot → write new snapshot + report. Fetch is shared across profiles; keep it that
way (one API call set per run, not per profile).

Tiered fetch strategy: (1) ATS JSON API [current], (2) HTTP + HTML parse, (3) Playwright.
Stay in tier 1 whenever possible — it's why this is low-maintenance.

**V2 additions (see @PROSPECTOR_V2_CHANGELOG.md):**
- `collect_pool` returns `(pool, errors, failed_companies)`. A company whose fetch RAISED
  contributed nothing, so `_run_lane` holds its previously-known roles out of the removal
  diff and carries them forward in the snapshot. **Never report a failed source's roles as
  removed** — that bug shipped to users before it was fixed.
- Per-profile display windows: the shared pool is fetched at the WIDEST window any profile
  asks for (`profile_age_window`), then narrowed per lane inside `_run_lane`. Lisa 14d,
  Chad 7d, one fetch.
- Two renderers. `build_html_report` = the original lane-by-lane email (Chad).
  `build_digest_html` = Lisa's ranked daily digest, selected by `report_style: "digest"`.
  Don't "unify" them without a reason; Chad's email is deliberately unchanged.
- The daily run writes diagnostics for the weekly audit: `rejects_<name>.json` (fetched but
  filtered out, with the rule that dropped each) and `source_health.json`
  (`consecutive_zero_runs` = ATS-migration signal). `audit.py` reads ONLY those + snapshots
  + feedback. Keeping the audit network-free is a deliberate constraint — a re-fetching
  audit would double our ATS traffic and risk the daily run.

## Files

- `companies.json` — `companies[]` + `needs_identification[]` backlog. Company fields: {name, city, ats, slug, domain}; **workday entries also need** {wd_host, site}. `domain` feeds logo prefetch.
- `remote_companies.json` — registry for the **US-remote lane** (same entry shape as `companies.json`). Hand-seeded, remote-friendly employers on tier-1 ATSes; each VERIFIED to return live US-remote roles before adding. Read only when `settings.remote_search.enabled` and a profile has `remote_search:true` (see US-remote lane below).
- `staffing_companies.json` — registry for the **contract/staffing lane** (same entry shape). Staffing/recruiting firms whose public feeds we monitor for **contract** roles. Read only when `settings.staffing_search.enabled` and a profile has `staffing_search:true` (see Contract/staffing lane below).
- `profiles.json` — `profiles[]` ({name, label, enabled, match_groups, exclude_any} + optional
  `mandate_rescue`, `report_style`, `max_posting_age_days`, `background_file`, `fit_mode`).
- `settings.json` — run-wide tweakables (loaded by `load_settings`, defaults in `SETTINGS_DEFAULTS`): `max_posting_age_days` (drop postings older than this; 0/null = keep all; unknown-date always kept), `fit_scoring_enabled` (master off-switch for the Anthropic API), and `star_within_days` (⭐ postings newer than this in the report; 0/null off — `main` sets the `STAR_WITHIN_DAYS` global from it). Missing file/keys fall back to defaults.
- `jobmonitor.py` — the engine. Key functions: `fetch_greenhouse/lever/smartrecruiters/workday`, `collect_pool`, `matches_profile`, `_mandate_rescue`, `enrich_salary`, `enrich_with_fit`, `score_fit`, `validate_verdict`, `diff`, `classify_removal`, `load_feedback`, `build_report`, `build_html_report`, `build_digest_html`, `run_profile`.
- `test_jobmonitor.py` — offline test suite. `audit.py` — weekly, network-free self-audit.
- `feedback_<name>.json` — hand-edited feedback (the only file a non-developer edits).
- `PROSPECTOR_V2_CHANGELOG.md` / `PROSPECTOR_TESTING.md` — what changed, and how to operate it.
- `fetch_logos.py` — occasional prefetch of company logos → `logos/<slug>.png` via logo.dev (needs `LOGO_DEV_TOKEN` env). **Not run by the daily job.**
- `logos/<slug>.png` — prefetched logos, committed; embedded inline in the email (see Company logos below).
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

`mandate_rescue` (optional, per profile; Lisa only) gives a SECOND chance to a role whose
title misses the groups: kept if the title still reads as leadership (`require_title_any`)
AND its **description** names >= `min_hits` distinct `terms`. Exclusions are checked FIRST, so
a rescue can never bypass them. Only DISTINCTIVE mandate language belongs in `terms` — generic
manager-JD boilerplate ("cross-functional", "stakeholder management", "program management")
was tried and rescued clear non-targets off real feeds. No description = no rescue, so the 11
Workday/SmartRecruiters companies (title-only at list time) can't benefit.

## Email output & company logos

`main` writes two per-profile outputs to `$GITHUB_OUTPUT` (when set): `<name>_changed`
(true/false — the workflow only emails a person when their report changed since last run;
first run counts as changed) and `<name>_logos` (comma-separated list of the exact logo
files that report referenced). The workflow gates each email step on `<name>_changed` and
passes `<name>_logos` to `attachments:`.

Logos render as a 40px tile left of each posting (`_logo_square`/`_icon_row` in
`build_html_report`): the prefetched `logos/<slug>.png` when it exists, else a colored
monogram of the company's initials (`_mono_color`/`_initials`). `_LOGOS_USED` (cleared at
the top of `build_html_report`) collects referenced files so **only referenced logos are
attached** — an attached-but-unreferenced file would show as a stray download. Logos embed
inline via **CID** (`<img src="cid:<slug>.png">`), which works because
`dawidd6/action-send-mail@v18` sets each attachment's Content-ID to its filename (undocumented
— re-verify on a major bump). The daily job never fetches logos; `fetch_logos.py` does that
occasionally (needs `LOGO_DEV_TOKEN`, kept out of the repo). Missing logo → monogram, so it
never breaks. Do NOT base64/data-URI logos instead — Gmail and Outlook strip `data:` images.

## US-remote lane (second search lane)

Additive to the local company search; never changes it. Master switch `settings.remote_search.enabled`;
a profile opts in with `remote_search:true`. Reads `remote_companies.json` (NOT `companies.json`)
and applies `is_us_remote` (keep US/remote; drop location-locked + non-US remote) instead of the
local `is_local` gate — `collect_pool(config_path, gate)` is parameterized for exactly this.
`_run_lane` matches/scores/diffs ONE lane against its own snapshot (`snapshot_<name>.json` local,
`snapshot_<name>_remote.json` remote — independent diffs); `run_profile` composes BOTH lanes into
**one report/email per person** (`build_report`/`build_html_report` take a list of lane dicts; multiple
lanes get "📍 Local"/"🌎 US-Remote" banners via `_lane_banner`). Each lane renders its own
"What's changed" (new + changed titles only) and "All current matching roles"; **removed/filled roles
are pulled out of every lane and collected in one "Removed / filled" region at the very bottom of the
report, with a per-lane sub-header** (`build_report`/`build_html_report`, driven by `lane["removed"]`).
`<name>_changed` = OR of the lanes (still counts removals, so a removal-only day still emails);
`<name>_logos` = union — so the **workflow email steps are unchanged** (one email, gated on the combined flag). Design + why the feed approach was dropped:
@DESIGN-remote.md. Grow the registry by verifying new remote-friendly employers on tier-1 ATSes
(same discipline as `companies.json`); `fetch_logos.py` already reads both registries.

## Contract/staffing lane (third search lane)

Additive, like the US-remote lane, and built on the same machinery. Master switch
`settings.staffing_search.enabled`; a profile opts in with `staffing_search:true` (currently
Lisa only). Reads `staffing_companies.json` and applies the **local gate `is_local`** (keeps
Utah-local + US-remote, drops other-metro on-site and international remote) — this is the
"US-remote + local" scope. `main` collects the staffing pool once (shared across profiles) and
`run_profile` runs it as a THIRD lane via the same `_run_lane` (suffix `_staffing`, snapshot
`snapshot_<name>_staffing.json`, banner "🧑‍💼 Contract / Staffing"). Optional
`settings.staffing_search.max_age_days` gives this lane its **own age window** (contract search
favors volume, so it's typically wider than the global `max_posting_age_days`; falls back to it
when unset). `<name>_changed` is now the
OR of up to three lanes; `<name>_logos` the union — so the **workflow email steps are unchanged**.
The lane reuses the profile's existing `match_groups`/`exclude_any` (no contract keyword group is
needed): **contract-only filtering happens in the fetcher**, on structured data, not the title.
Purpose: an income-focused contract search alongside the leadership search — the monitor is the
trip-wire ("a matching contract posting appeared"), not a recruiter-outreach CRM (out of scope).
Current firms: **Aquent** (RSS) + **Eliassen** & **Addison Group** (SnapHop Atom, via `fetch_snaphop`).

**Hard-won finding on growing this lane (probed 2026-07, ~37 firms across two sweeps):** staffing
firms do NOT expose the raw Bullhorn REST API — even the ones on Bullhorn (Motion Recruitment:
`bte.bullhornstaffing.com`) wrap it **server-side** and expose no public `corpToken`/REST. The one
generalizable tier-1 surface is a **per-firm RSS/Atom job feed**, and those are RARE: only **3**
of ~37 had one — Aquent (`/feeds/jobs.xml`) and two on the **SnapHop** careersite platform
(`careers.<domain>/feeds/jobs.atom` — Eliassen, Addison). SnapHop is the one reusable vendor, so
`fetch_snaphop` is the generalization payoff (new SnapHop firm = one registry row). Everyone else
probed (Robert Half, Randstad, Mondo, Beacon Hill, Kforce, Vaco, Judge, Collabera, Onward Search,
24 Seven, Creative Circle, LaSalle, …) had **no job feed** (JS app/gated), or only a **blog** `/rss`
(LaSalle, Creative Circle, Insight Global). Motion Recruitment is server-rendered + tech-only
(off-domain for Lisa). TEKsystems (Allegis, bot-blocks scripts) and Insight Global (custom ASP.NET;
iCIMS = corporate-hiring only) stay deferred. **To add a firm: probe `careers.<domain>/feeds/jobs.atom`
(SnapHop) and `<domain>/feeds/jobs.{xml,rss}` first — only a real job feed is a clean tier-1 add;
otherwise it needs brittle tier-2 HTML scraping and probably isn't worth it.**

## Location + age gates

Global, applied once to the pool in `collect_pool` before profiles. Location:
`LOCAL_KEYWORDS`, `KEEP_REMOTE`, `LOCAL_ONLY` (constants atop `jobmonitor.py`). `is_local`
matches keywords with `\bword\b` boundaries (like `matches_profile`) — do NOT revert to
substring `in`, or the short `ut` token matches inside foreign words ("So**ut**hampton").
`KEEP_REMOTE` keeps remote roles, but when `allow_international_remote` (settings.json, default
false) is off, a remote role naming a non-US country (`INTERNATIONAL_MARKERS`, e.g. "United
Kingdom - Remote") is dropped unless it ALSO names a local city. Markers that collide with US
places are intentionally omitted (Georgia, Mexico→New Mexico, Jordan→South Jordan). Age:
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
- **Recruitee**: `fetch_recruitee` IMPLEMENTED + registered. `GET {slug}.recruitee.com/api/offers/` → `{offers:[{id,title,location,city,country,careers_url,published_at,description,remote,salary}]}`. JSON, no auth; 404s on bad slug. Config `{ats:"recruitee", slug}`. NOTE: Recruitee is EU-centric — probed candidates had ~0 US-remote roles, so the fetcher exists for capability/local use, not because it currently feeds the remote registry.
- **Personio**: `fetch_personio` IMPLEMENTED + registered. `GET {slug}.jobs.personio.com/xml?language=en` → **XML** `<position>` elements (id,name,office,additionalOffices,department,createdAt,jobDescriptions). No JSON, no auth, and NO apply URL in the feed — the job URL is built as `{slug}.jobs.personio.com/job/{id}`. Parsed via stdlib `xml.etree` + `_get_text` (raw-bytes fetch). Config `{ats:"personio", slug}`. NOTE: German/EU-centric — ~0 US-remote roles in practice; capability only.
- **Ashby**: `fetch_ashby` is IMPLEMENTED and registered. `GET api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` → `{jobs:[{id,title,location,isRemote,jobUrl,publishedAt,descriptionPlain,compensation}]}`. `publishedAt` → `posted`; `descriptionPlain` feeds LLM/salary; salary comes from `compensation.compensationTierSummary` (trimmed at the first `•` to drop equity/benefits prose). 404s on bad slug. Used mainly by the US-remote registry (Supabase, Replit, Ramp, Vanta, Sentry, Notion, …).
- **Aquent** (staffing lane): `fetch_aquent` IMPLEMENTED + registered. `GET aquent.com/feeds/jobs.xml` → **RSS/XML** `<item>` elements: {job_id, title ("Role [job_id]"), location{city,state,country}, placement_type, remotetype, salary ("$45-48 Hourly"), description (HTML), pubDate (RFC-822), url}. Parsed via stdlib `xml.etree` (raw-bytes fetch, like Personio). ONE national feed — no per-company slug (config `{ats:"aquent","slug":"aquent"}`; slug is registry/logo identity only, optional `"feed_url"` overrides). Specifics: **only contract placement_types kept** (`_CONTRACT_PLACEMENT` — this is the contract lane); **non-US dropped by the structured `country` code** (the string gate's markers miss codes like "FR"/"GB", and 2-letter codes collide with English words, so filter here); `remotetype` folded into the location so the gate keeps remote roles; salary kept only if it names a currency (the field sometimes carries "W2"/"part-time" notes); CDATA titles `html.unescape`d.
- **SnapHop** (staffing lane, shared vendor): `fetch_snaphop` IMPLEMENTED + registered — one fetcher for every staffing firm on the **SnapHop careersite** (Bullhorn-for-Salesforce front end; feed host `<slug>.gosnaphop.com`). Currently **Eliassen** + **Addison Group**. `GET careers.{domain}/feeds/jobs.atom` (derived from the entry's `domain`; override with `feed_url`) → **Atom** `<entry>`s {id ("tag:…:/<uuid>" = stable key), title, summary (HTML), published/updated (ISO), link href (slug ends `<city>-<state>` or `-anywhere`, then a Salesforce id)}. Returns the ~100 most-recent roles (**no pagination**). **NO structured location/type/salary**: location via `_snaphop_location` (remote iff slug `-anywhere` / "remote" in the TITLE / summary-first-line is exactly a remote phrase — NOT any "remote" deep in the summary, which would misread Eliassen hybrids; else "City, ST" from the summary, else the slug tail — 2-letter code OR full state name via `_US_STATE_ABBR`/`_US_STATE_NAMES`); contract = keep-unless-permanent (`_PERMANENT_MARKERS`); salary left to `enrich_salary`. **Adding a SnapHop firm = one registry row** `{ats:"snaphop","slug":<logo id>,"domain":<firm domain>}` (verify `careers.<domain>/feeds/jobs.atom` returns entries first).
- **Phenom** (phenompeople.com careersite): `fetch_phenom` IMPLEMENTED + registered. `POST https://{host}/widgets` (host defaults to `careers.{domain}`) with a paged `refineSearch` JSON body (`_phenom_payload`, `from`/`size`) → `{refineSearch:{totalHits, data:{jobs:[{jobId,title,cityStateCountry,isMultiLocation,multi_location,applyUrl,postedDate,descriptionTeaser}]}}}`. No auth; paginate `from` until `>= totalHits`. Stable id = `jobId`. It's a **search front end over a real ATS** (Circle's `applyUrl`s are Workday), so `description` is only the short `descriptionTeaser`. **GOTCHA (cost us a think):** the location field carries **no "remote"** even for remote roles — remote lives only in the Workday `applyUrl` path (`…-remote-first-in-US`), so `fetch_phenom` folds `(Remote)` into the location when the applyUrl says remote, which is what lets `is_us_remote` keep US-remote roles (and still drop India/Singapore remote). Config `{ats:"phenom","slug":<logo id>,"domain":<firm domain>[,"phenom_host":<careers host if not careers.{domain}>]}`. Used by the US-remote registry (Circle).
- Only reach for Playwright if a company has no reachable JSON at all.

Each new ATS = one `fetch_*` function returning normalized postings + one `FETCHERS` entry.

## LLM fit scoring (optional relevance filter)

Ranks each profile's matched roles by how well they fit a candidate, via the Anthropic API.
- **Activation:** on only when `settings.json` `fit_scoring_enabled` is true (master off-switch — set false to skip all API calls for test runs) AND `ANTHROPIC_API_KEY` is set (env) AND the profile has a `background_file`. Otherwise the whole feature no-ops and the tool behaves exactly as before. `anthropic` SDK must be installed (`pip install anthropic`) — CI needs this in the workflow.
- **Config:** profile fields `background_file` (path to a candidate JSON, e.g. `lisa_background.json`) and `fit_mode` = `"rank"` (keep all, sort by score — default/safe) or `"filter"` (drop model-rated `"no"`).
- **Flow:** `enrich_with_fit` runs after keyword match, before diff. It **only scores NEW roles** — verdicts are cached in the snapshot's `fit_result` and reused, so daily cost ∝ new postings, not total. `score_fit` returns `{fit: yes|maybe|no, score: 0-100, reason}`; on ANY API/parse failure it returns a neutral `maybe`/score -1 (role kept, run never crashes) — and **failures aren't cached** (`score < 0` is never reused), so a transient parse error retries next run.
- **Cache invalidation:** each verdict stores a `bg` fingerprint (`_bg_fingerprint` = short hash of the background content). A cached verdict is reused only if its `bg` matches the current background. **Editing a `background_file` auto-invalidates that profile's verdicts** → full re-score next run, no manual clearing. Legacy verdicts with no `bg` also re-score once.
- **Descriptions:** `fetch_greenhouse` (`?content=true`, HTML-cleaned) and `fetch_lever` (`descriptionPlain`) now include a `description` field for better judgments. SmartRecruiters/Workday are title-only (no cheap bulk description). `description` is stripped before the snapshot is written (kept lean); `fit_result` is persisted.
- **Model:** `FIT_MODEL` constant (currently `claude-sonnet-5`; `claude-haiku-4-5` for lower cost). Verify current model strings against docs.claude.com.
- **V2 verdict shape:** `score_fit` returns `qualification_fit`, `interest_fit`,
  `practical_fit`, `opportunity_score`, `recommendation` (apply_first | strong_fit | stretch |
  practical_contract | not_recommended), `reasons[<=5]`, `concerns[<=3]`, and three
  explicit-mention booleans. `validate_verdict` REJECTS the verdict if any of the four scores
  or the recommendation is malformed (a wrong recommendation is worse than none) and returns a
  neutral `score -1` that is never cached. Legacy `fit`/`score`/`reason` aliases are still
  populated so Chad's renderer and `fit_mode:"filter"` keep working — don't remove them.
- **`FIT_SCHEMA_VERSION`** is folded into the cache fingerprint. **Bump it whenever the verdict
  fields or their meaning change**, or new code will read stale old-shape verdicts.
- **Prompt caching:** rubric + candidate go in a cached `system` block (`_fit_system`,
  `sort_keys=True` so the bytes are stable); only the posting varies per call. A cache miss is
  harmless. Per-run usage prints via `fit_usage_summary()`.
- `not_recommended` roles are hidden at RENDER time but kept in the snapshot for the audit —
  which is why Lisa's `fit_mode` stays `"rank"`, not `"filter"`.

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
