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
- `python3 test_jobmonitor.py` — 223 offline tests (stdlib `unittest`; no network, no API).
- `python3 audit.py` — weekly self-audit. **Reads committed files only; makes no network
  calls of any kind.**

**V3 additions (see @PROSPECTOR_V3.md — read it before touching discovery, lifecycle or the
digest):** discovery gate separated from fit scoring (`classify_match` → `core`/`discovery`
tiers); persistent lifecycle DB in `lifecycle.py` (`jobs_<profile>.json`, one record per REAL
job, cross-feed dedup); four-state source health with retry; the change digest
(`report_style:"change"`); Discovery Log export in `sheets_sync.py`. Four sources added
(Robert Half, Himalayas, We Work Remotely, Jobicy). **Kforce was probed and rejected** — no
feed, no sitemap, Azure Search behind a client API key, `robots.txt` disallows its service
paths. Do not spend another session on it.

**V4 additions — the priority-employer lane (read before touching it):** a fourth search lane
for employers the reader named explicitly rather than found by search. Registry
`priority_companies.json`, master switch `settings.priority_search.enabled`, profile opt-in
`priority_search:true` (Lisa only). Three new fetchers — **Oracle Recruiting Cloud**
(`fetch_oracle`, a shared vendor), **Paradox.ai careersites** (`fetch_paradox`, a shared
vendor) and **Atlassian** (`fetch_atlassian`). Named geography regions (`REGION_GATES`,
`region_gate`, `location_rank`) add **Washington** as a third location tier WITHOUT touching
the shared local gate. `fetch_workday` gained `search_texts` (a list). See
**Priority-employer lane** below.

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
- `priority_companies.json` — registry for the **priority-employer lane** (same entry shape, plus `priority`, `note`, `exclude_titles`). Employers the reader named explicitly; each probed live before adding. Read only when `settings.priority_search.enabled` and a profile has `priority_search:true` (see Priority-employer lane below).
- `profiles.json` — `profiles[]` ({name, label, enabled, match_groups, exclude_any} + optional
  `mandate_rescue`, `report_style`, `max_posting_age_days`, `background_file`, `fit_mode`, `priority_search`, `email`).
- `settings.json` — run-wide tweakables (loaded by `load_settings`, defaults in `SETTINGS_DEFAULTS`): `max_posting_age_days` (drop postings older than this; 0/null = keep all; unknown-date always kept), `fit_scoring_enabled` (master off-switch for the Anthropic API), and `star_within_days` (⭐ postings newer than this in the report; 0/null off — `main` sets the `STAR_WITHIN_DAYS` global from it). **`email.enabled`** is the run-wide master switch for SENDING reports (per-person delivery is `email` in `profiles.json`). **`location_priority`** is the reader's location/work-arrangement preference, BEST FIRST (`["remote_us","utah","washington"]`) — one setting doing two jobs so the order can never disagree with itself: it is the priority lane's geography AND the digest's ranking tiebreak within a fit score. Missing file/keys fall back to defaults.
- `jobmonitor.py` — the engine. Key functions: `fetch_greenhouse/lever/smartrecruiters/workday`, `collect_sources`/`collect_pool`, `fetch_source`, `classify_fetch_error`, `matches_profile`, `classify_match`, `_mandate_rescue`, `_apply_source_rules`, `region_gate`/`is_washington`/`location_rank`, `enrich_salary`, `enrich_with_fit`, `score_fit`, `validate_verdict`, `diff`, `classify_removal`, `load_feedback`, `update_lifecycle`, `email_enabled_for`, `github_output_lines`, `change_sections`, `build_report`, `build_html_report`, `build_digest_html`, `build_change_digest_html`, `run_profile`.
- `lifecycle.py` — the persistent discovery database (V3). `dedupe_key`, `upsert`, `refresh_statuses`, `close_missing`, `mark_shown`, `run_verification`, `prune`. **`mark_shown` is the ONLY thing that sets `ever_shown`**, and `ever_shown` is the ONLY thing that makes a role eligible for the digest's removed section — that is the fix for "removed roles that were never emailed". **`close_missing` must never mark a job removed on the word of a failed source.**
- `sheets_sync.py` — Discovery Log export: CSV always, Apps Script webhook (`SHEETS_WEBHOOK_URL`) when configured. Writes ONLY the `Prospector Discovery Log` tab — never `Application Pipeline` or `Applied`.
- `jobs_<profile>.json` — **STATE.** The lifecycle DB. Committed by CI. Do not delete: it carries `first_seen`, `ever_shown` and the human decision fields.
- `test_jobmonitor.py` — offline test suite. `audit.py` — weekly, network-free self-audit.
- `feedback_<name>.json` — hand-edited feedback (the only file a non-developer edits).
- `PROSPECTOR_V2_CHANGELOG.md` / `PROSPECTOR_TESTING.md` — what changed, and how to operate it.
- `fetch_logos.py` — occasional prefetch of company logos → `logos/<slug>.png` via logo.dev (needs `LOGO_DEV_TOKEN` env — the **publishable** `pk_` token; it is passed as a query param to `img.logo.dev`, so the server-side `sk_` key will not work). **Not run by the daily job.** Run it locally, or use the manual `prospector-logos` workflow (`.github/workflows/logos.yml`, `LOGO_DEV_TOKEN` repo secret) which fetches, verifies and commits for you.
- `.github/scripts/verify_logos.py` — gate for that workflow. It QUARANTINES rather than failing: an HTML error page or truncated body is deleted so it can never be committed, the rest still land, and the affected company falls back to its monogram tile and retries next run. Hard-failing was rejected because one flaky domain would discard every good logo in the batch.
- `logos/<slug>.png` — prefetched logos, committed; embedded inline in the email (see Company logos below).
- `snapshot_<name>.json` (+ `_remote`, `_staffing`, `_priority` variants), `report_<name>.md`, `report_<name>.html` — generated per profile; committed by CI.

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

**Corollary worth knowing before you edit any term list: a STEM can never match.** Because
every list here is matched as `\bterm\b`, `"housekeep"` does not match "Housekeeping" and
`"veterinar"` does not match "Veterinarian". A dead term is invisible — the list looks like it
is filtering and silently is not. **`profiles.json` `discovery.exclude_any` currently carries
four dead terms** — `housekeep`, `veterinar`, `phlebotom`, `radiolog` — left in place because
Lisa asked for her existing criteria to be unchanged; the practical gap they leave for hotel
property roles is closed by `exclude_titles` on the Marriott and Hilton rows. Write complete
words (`housekeeping`, `housekeeper`) in any new list. `TestPriorityRegistry.test_no_exclude_title_is_a_dead_stem`
enforces this for the priority registry.

`dedupe_same_title` (optional, per profile; Lisa only) collapses one-req-per-city duplicates
via `dedupe_same_role` BEFORE scoring — employers open a separate requisition per location for
a single opening (Angi posts one "Manager, Retail Partnerships" four times), so without this
the email shows N identical cards and we pay to score each. Keeps the earliest-posted copy.
**Chad deliberately does NOT use it**: two genuinely different "Software Engineer" openings
legitimately share a title.

Leveled IC tracks are excluded by WORD ORDER, which is worth understanding before editing
`exclude_any`: `"customer success manager"` drops the IC ladder (Customer Success Manager
II/III/V, Enterprise CSM, Strategic CSM) while KEEPING "Sr. Manager, Customer Success",
"Director, Customer Success" and "Head of Customer Success", because leadership titles put the
seniority word first. Found by probing Samsara, which alone would have added ~8 IC CSM roles.

`mandate_rescue` (optional, per profile; Lisa only) gives a SECOND chance to a role whose
title misses the groups: kept if the title still reads as leadership (`require_title_any`)
AND its **description** names >= `min_hits` distinct `terms`. Exclusions are checked FIRST, so
a rescue can never bypass them. Only DISTINCTIVE mandate language belongs in `terms` — generic
manager-JD boilerplate ("cross-functional", "stakeholder management", "program management")
was tried and rescued clear non-targets off real feeds. No description = no rescue, so the 11
Workday/SmartRecruiters companies (title-only at list time) can't benefit.

## Email output & company logos

`main` writes THREE per-profile outputs to `$GITHUB_OUTPUT` (when set), all built by
`github_output_lines`: `<name>_changed` (true/false — the workflow only emails a person when
their report changed since last run; first run counts as changed), `<name>_email` (true/false
— is delivery switched on for this person), and `<name>_logos` (comma-separated list of the
exact logo files that report referenced). The workflow gates each email step on
**`<name>_email` AND `<name>_changed`** and passes `<name>_logos` to `attachments:`.

**Pausing one person's email** (`email: false` in `profiles.json`; master switch
`settings.json` `email.enabled`). **Chad is currently paused; Lisa is on.** Three things
about it are deliberate and easy to get wrong:

- It is **delivery only**. A paused profile still fetches, diffs, scores, writes its snapshot
  and lifecycle DB, and writes `report_<name>.md/.html` into the repo — read them there any
  time. This is NOT the same as `enabled: false`, which stops the work: a profile that stops
  running freezes its snapshot, so the first run after switching it back on diffs against a
  stale snapshot and lands as one enormous months-long "new roles" email. **Pause with
  `email`, not `enabled`,** unless you also want the work stopped and accept that backlog.
- `<name>_email` is a **separate output**, not `<name>_changed` forced to false. Folding them
  together would make the run log claim a report did not change when it did, and — because
  the workflow's `force_email` input ORs against `_changed` — would let a manual test run
  silently defeat a deliberate pause. The gate is ANDed OUTSIDE the `force_email` group for
  exactly that reason; `TestWorkflowGating` fails if it is moved inside.
- A paused profile is **never confirmed as delivered**. `--confirm-sent` keys off the email
  step's `outcome == 'success'`, and a skipped step is not a success, so its `shown` marks
  stay pending — correct, because nobody saw those roles and none may later turn up in a
  "Removed since prior run" section. This falls out of the existing check with no extra code.

`python3 jobmonitor.py --list` shows each profile's email state, because "is my report still
being sent?" is the question a pause creates and `profiles.json` is not where a
non-developer looks.

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

**Third sweep (2026-07-31), 22 more firms, ZERO feeds found** — Signature Consultants, Apex
Systems, Yoh, Modis, Experis, Hays, Michael Page, Robert Walters, Korn Ferry, Heidrick &
Struggles, Solomon Page, Atrium, CyberCoders, Talently, Bolt Staffing, Cella, Career Profiles,
Mondo, Hired by Matrix, Sparks Group, Robert Half/Creative Group, Nesco Resource. Running
total across three sweeps: **3 feeds out of ~59 firms probed.** Treat the contract lane as
EFFECTIVELY CAPPED at its current 3 firms unless a new SnapHop-style shared vendor appears —
do not spend another session probing staffing firms one by one.

## Priority-employer lane (fourth search lane)

Additive, like the US-remote and contract lanes, and built on the same `_run_lane` machinery.
Master switch `settings.priority_search.enabled`; a profile opts in with
`priority_search:true` (Lisa only). Reads `priority_companies.json`, suffix `_priority`,
snapshot `snapshot_<name>_priority.json`, banner "⭐ Priority employers".

**What it is for.** These are employers the reader NAMED — a referral, a recruiting contact,
a deliberate target — rather than employers a search found. Current eight: **Adobe,
Salesforce, Zillow Group, Atlassian, NPR, PBS, Marriott International, Hilton.** All were
probed live 2026-08-25; the per-row `_comment` in the registry records what each surface is
and why it was chosen over the alternatives.

**Its geography is `settings.location_priority`, not the local gate** — by default
US-remote + Utah + Washington (`region_gate(...)`). This is why the lane exists as a lane
rather than as extra rows in `companies.json`: `is_local` is shared by every profile, so
widening it to a third state would silently have changed Chad's report too, and adding these
employers to `companies.json` would have handed him four large tech boards he never asked
for. **Chad opts into nothing here and his retrieval and email are byte-for-byte unchanged.**

**It changes RETRIEVAL, not judgment.** In `classify_match`, a posting carrying
`priority_employer` is retrieved on ONE signal instead of two: a role family in the title, OR
the mandate appearing in the description. Everything downstream is identical — `hard_excluded`
still applies, nothing is promoted to the core tier, a precision-rule hit still demotes to
wildcards, and the model scores it on the same rubric.

**SENIORITY ALONE IS DELIBERATELY NOT A SIGNAL, and this is the trap.** `seniority_any`
contains "enterprise", "global", "senior", "manager" and "director" — words in essentially
every corporate title — so accepting them on their own is not a relevance signal, it is
"this posting is a job". Measured against the live pool it retrieved **231 roles,
overwhelmingly enterprise SALES** ("Enterprise Sales Account Director"), a function Lisa's
background lists under `weak_or_wrong_fit`, at real per-role scoring cost. Likewise the
one-hit description path reads **`mandate_rescue.terms`**, not `description_terms`: the
latter is tuned for a two-hit threshold and carries generic words ("adoption", "governance",
"okrs", "board") that pulled in Atlassian sales roles when any single one counted. Override
with `discovery.priority_terms` if the two ever need to diverge.

**Measured effect** (live pool, 2026-08-25): 740 in-region postings → 125 retrieved after
dedup, of which only **15 were net-new from the relaxation**; the rest would have matched
anyway. That ratio is the point — this is a recall fix, not a floodgate.

**Registry fields beyond the usual shape:**
- `priority: true` — stamps `priority_employer` on every posting from the row
  (`_apply_source_rules`). **NOT called `priority`** on the posting/record: `lifecycle` already
  uses that key for its P1/P2/P3 urgency band, and overwriting it breaks digest ordering.
  The flag is inert for a profile with no `discovery` block, which is why Adobe can carry it
  in `companies.json` without affecting Chad.
- `note` — the reader's own sentence about the employer (e.g. "you have a referral here"),
  rendered verbatim on the digest card. It is not a model judgment and is never folded into
  `why_fits`.
- `exclude_titles` — titles THIS employer posts that the reader never wants, dropped before
  any shared gate. It is per-employer on purpose: Marriott and Hilton want corporate roles and
  not property operations, and "Director of Housekeeping" is a perfectly good leadership title
  a profile-wide rule would have to keep. **Matching is whole-word (`\bterm\b`, like
  everything else here), so write COMPLETE words** — `"housekeep"` matches nothing at all.

**Adding an employer:** probe the surface first (never guess a Workday tenant or an Oracle
org id), add the row, then confirm with a `--dry-run`. `test_jobmonitor.py` binds to the
committed registry, so a typo fails a test rather than a 6am cron.

**Known limit, stated plainly:** Workday exposes no description at list time, so for the
Workday priority employers (Adobe, Salesforce, Zillow, PBS) the relaxation reduces to the
role-family path — the same limit V3 documents for SmartRecruiters/Workday generally. It is
still materially wider than core, which demands family AND seniority together.

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
add a per-profile override rather than widening the global list.

**Named regions (V4)** are that per-profile override, now built: `REGION_GATES` maps
`remote_us`/`utah`/`washington` to `is_us_remote`/`is_utah`/`is_washington`, `region_gate(regions)`
composes them into one gate, and `location_rank(loc, regions)` gives each location a position
in the preference order. `is_local` itself is UNCHANGED — a lane opts into regions, so
widening one lane's geography can never widen another profile's. `location_rank` is a
**tiebreak inside a fit score, never a filter**: ranking on location first would push a strong
Utah-hybrid role below a weak remote one.

**`is_washington` is Washington STATE, not Washington DC**, and that distinction is load
bearing — NPR, PBS, Marriott and Salesforce all post heavily in DC, and Adobe's own
`searchText=Washington` returns DC roles, so matching the bare word would quietly import a
whole second metro. `_DC_MARKERS` guards it, falling back to the unambiguous city names so a
multi-site "Seattle, WA; Washington, DC" req is still kept. `"vancouver"` is deliberately
omitted (Vancouver BC dwarfs Vancouver WA in feeds). Note: a role that ages out
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
- **Oracle Recruiting Cloud** (Fusion "CandidateExperience", `*.oraclecloud.com`): `fetch_oracle` IMPLEMENTED + registered. A **shared vendor** like SnapHop — one fetcher, many employers (Hilton and Marriott are both on it, as is much of the Fortune 500). `GET https://{oracle_host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},facetsList=...,limit=N,offset=M,sortBy=POSTING_DATES_DESC` → `{items:[{TotalJobsCount, requisitionList:[{Id,Title,PostedDate,PrimaryLocation,PrimaryLocationCountry,WorkplaceType,ShortDescriptionStr}], organizationsFacet, categoriesFacet, …}]}`. No auth. **`limit` genuinely honours 200** (unlike Workday) so a board is a couple of requests. Config `{ats:"oracle","slug","domain","oracle_host","site"[,"organizations":[ids],"page_size","max_pages"]}`.
  - **The `organizations` facet is how you get corporate roles out of an operations-heavy tenant.** Marriott exposes `Corporate` (337) beside `Marriott Hotels & Resorts` (2893) etc.; Hilton exposes `US Corporate/Executive` (81) beside `AMERICAS`/`APAC`/`EMEA`. Selecting the corporate ids does the corporate/property split **on the employer's own classification**, server-side, instead of us guessing from titles. Fetch page 0 with `facetsList=ORGANIZATIONS` to read the ids.
  - **GOTCHA:** multiple org ids join with **`;`**. A comma silently keeps only the FIRST id — it looks like a working filter and is not.
  - **GOTCHA:** the work arrangement lives in `WorkplaceType`, NEVER in the location string, so `fetch_oracle` folds `(Remote)`/`(Hybrid)` into the location — otherwise the gates read a fully-remote role as onsite-at-HQ. Non-US is dropped on the structured `PrimaryLocationCountry`, like `fetch_aquent`, because the string markers deliberately omit "Mexico".
- **Paradox.ai careersites** (`careers.<domain>`): `fetch_paradox` IMPLEMENTED + registered. The second shared vendor; Marriott International runs on it. `POST https://{host}/api/get-jobs?<query string>` with body `{}`, where the query string carries `filter[<facet>][<n>]=<value>`, `page_number`, `page_size` → `{totalJob, jobs:[{requisitionID,title,description,locations[],isRemote,applyURL,originalURL,customFields,employmentType}]}`. Rich: **full description** and often a pay range in `customFields.cf_titleinfo`. Config `{ats:"paradox","slug","domain"[,"paradox_host","queries":[{"filter":{...}}],"max_pages"]}`.
  - **GOTCHA:** answers **403 to a cold request** — it needs the `ct` cookie any page on the site sets, so `_paradox_opener` GETs `/jobs` once per run and reuses the jar.
  - **GOTCHA:** `page_size` is **clamped to 10** server-side, so `max_pages` is the real budget. The HTML page is worse: it server-renders only 10 results into `window.__PRELOAD_STATE__` and ignores `page_size` entirely, so scraping it would mean ~34 one-megabyte page loads per employer.
  - **GOTCHA:** `sort_by` accepts only the site's own vocabulary and **400s** on anything else (`date`, `posted_date` both fail). We don't send it. There is **no posting date at all** in the payload — `posted` is left empty on purpose, and the digest labels those as first-seen.
- **Atlassian**: `fetch_atlassian` IMPLEMENTED + registered. `GET www.atlassian.com/endpoint/careers/listings` → a JSON array of ~250 postings with `{id,title,locations[],applyUrl,overview,responsibilities,qualifications,compensation,portalJobPost}`. One request for the whole board. Its real ATS is iCIMS (`globalcareers-atlassian.icims.com`), which has no public API, so this is the supported surface. Config `{ats:"atlassian","slug","domain"}`.
  - `posted` is left EMPTY deliberately: the feed carries only `portalJobPost.updatedDate`, an EDIT timestamp — a two-year-old role re-touched yesterday would read as posted yesterday, the exact "stale roles resurfacing as new" failure V3 exists to remove.
  - `compensation` is **not** a pay range; it is pay-zone/benefits prose (1 of 248 postings had a parseable figure), so it is regexed with `_PAY_RE` and dropped unless it really states money.
  - `locations` entries are `"<City> - <Country> - <full street address>"` and a role lists every eligible site plus a bare `"Remote - Remote"`. `_atlassian_location` keeps the first TWO segments per entry — trimming to the city alone would turn `"Bengaluru - India - …"` into `"Bengaluru"`, which names no international marker, and `is_us_remote` would then read an India-only remote role as US-eligible.
- **Workday `search_texts`** (a LIST) unions several server-side scopes, deduplicated by req number; `search_text` remains the single-term spelling and both may be given. It exists because Workday **silently caps `limit` at 20 no matter what you send** (100 → 0 results), so a big tenant must be scoped: Salesforce is 1,530 roles globally but a few hundred across Remote + Bellevue + Seattle. Do not try raising `limit` again.
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
3. Fix the four dead stem terms in `profiles.json` `discovery.exclude_any` (`housekeep`,
   `veterinar`, `phlebotom`, `radiolog`) — they match nothing today. Needs Lisa's sign-off
   because it changes her committed criteria. See Profile matching semantics.
4. Source-health alert when a known company's endpoint drops to zero (likely ATS migration).
5. Optional HTML email bodies (nice with the ranked/scored layout).
