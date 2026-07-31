# Prospector — Discovery Document

Current-state documentation of the Prospector job monitor, written for an AI assistant
that needs to understand the system before recommending changes. **Describes the
implementation as it exists; no recommendations.**

**What it is:** a single Python 3.12 script (`jobmonitor.py`, ~1,437 lines, standard
library only + optional `anthropic` SDK) run once per day by GitHub Actions
(`.github/workflows/prospector.yml`, cron `0 13 * * *` = 7:00 AM Mountain/MDT). It
fetches every configured company's open roles from that company's ATS, applies global
location + age gates, then for each named "profile" (person) applies a title filter,
LLM fit-scores the new roles, diffs against that profile's last snapshot, and emails
one HTML report per person. Two profiles exist: `chad` and `lisa`.

**Run modes:** `python3 jobmonitor.py` (all enabled profiles) ·
`--profile <name>` (one) · `--list`.

**Three search lanes**, each with its own registry file, its own location gate, and its
own snapshot, all composed into ONE email per person:

| Lane | Registry | Gate | Snapshot suffix | Enabled by |
|---|---|---|---|---|
| 📍 Local — Silicon Slopes | `companies.json` (38 cos.) | `is_local` | *(none)* | always on |
| 🌎 US-Remote | `remote_companies.json` (71 cos.) | `is_us_remote` | `_remote` | `settings.remote_search.enabled` + profile `remote_search:true` |
| 🧑‍💼 Contract / Staffing | `staffing_companies.json` (3 firms) | `is_local` | `_staffing` | `settings.staffing_search.enabled` + profile `staffing_search:true` |

Fetch happens **once per run** and is shared across profiles (one API call set per run,
not per profile).

---

# Search Sources

All sources are structured endpoints (JSON, XML/RSS, or Atom). No HTML scraping, no
Playwright, no browser automation. `{slug}` values come from the registry files.

## Source types (fetchers in `jobmonitor.py`, registered in `FETCHERS`)

| ATS / vendor | Type | Endpoint | Fetcher |
|---|---|---|---|
| Greenhouse | JSON API (GET) | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | `fetch_greenhouse` |
| Lever | JSON API (GET) | `https://api.lever.co/v0/postings/{slug}?mode=json` | `fetch_lever` |
| SmartRecruiters | JSON API (GET, paginated `offset`) | `https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N` | `fetch_smartrecruiters` |
| Workday | JSON API (**POST**, paginated `offset`, limit 20) | `https://{wd_host}/wday/cxs/{tenant}/{site}/jobs` | `fetch_workday` |
| Ashby | JSON API (GET) | `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | `fetch_ashby` |
| Recruitee | JSON API (GET) | `https://{slug}.recruitee.com/api/offers/` | `fetch_recruitee` |
| Personio | **XML** feed (GET) | `https://{slug}.jobs.personio.com/xml?language=en` | `fetch_personio` |
| Aquent | **RSS/XML** feed (GET) | `https://aquent.com/feeds/jobs.xml` | `fetch_aquent` |
| SnapHop | **Atom** feed (GET) | `https://careers.{domain}/feeds/jobs.atom` | `fetch_snaphop` |
| Phenom | JSON API (**POST** `refineSearch`, paginated `from`/`size` 100) | `https://{phenom_host or careers.{domain}}/widgets` | `fetch_phenom` |

Recruitee and Personio fetchers exist but **no registry entry currently uses them**
(capability only; both are EU-centric).

Per-posting **detail endpoints** are called only as a salary fallback, only for roles a
profile already matched (`_detail_text`):
Greenhouse `…/jobs/{id}` (`content`) · SmartRecruiters `…/postings/{id}` (`jobAd.sections`) ·
Workday `https://{wd_host}/wday/cxs/{tenant}/{site}{externalPath}` (`jobPostingInfo.jobDescription`).

## Lane 1 — Local (`companies.json`, 38 companies)

23 Greenhouse, 8 Workday, 4 Lever, 3 SmartRecruiters.

- **Greenhouse** (`boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`):
  Qualtrics `qualtrics` · DigiCert `digicert` · Lucid Software `lucidsoftware` ·
  Galileo (SoFi) `galileo` · Awardco `awardco` · Weave `weave` · Route `route` ·
  Podium `podium81` · MX `mxtechnologiesinc` · BILL `billcom` ·
  Recursion `recursionpharmaceuticals` · Canopy `canopytax` · Nav `navtechnologies` ·
  BambooHR `bamboohr17` · Traeger `traegergrills` · Beyond (Overstock) `beyond` ·
  NICE `nice` · Brex `brex` · Kelso Industries `kelsoindustries` · Speechify `speechify` ·
  Flex `flex` · iCapital `icapitalnetwork` · LVT `liveviewtechnologiesinc`
- **Lever** (`api.lever.co/v0/postings/{slug}?mode=json`): Filevine `filevine` ·
  Entrata `entrata` · Nomi Health `nomihealth` · Pattern `pattern`
- **SmartRecruiters** (`api.smartrecruiters.com/v1/companies/{slug}/postings`):
  Cricut `cricut` · Vivint `vivint` · Instructure `instructure`
- **Workday** (`POST https://{wd_host}/wday/cxs/{slug}/{site}/jobs`):
  Pluralsight `pluralsight.wd1.myworkdayjobs.com/Careers` ·
  Domo `domo.wd12.myworkdayjobs.com/DomoCareers` ·
  Adobe `adobe.wd5.myworkdayjobs.com/external_experienced` (**`search_text:"Lehi"`** — the
  only server-side keyword scoping in the project; narrows ~897 roles to ~65) ·
  Ancestry `ancestry.wd501.myworkdayjobs.com/Careers` ·
  Health Catalyst `healthcatalyst.wd5.myworkdayjobs.com/healthcatalystcareers` ·
  Western Governors University `wgu.wd5.myworkdayjobs.com/External` ·
  O.C. Tanner `octanner.wd501.myworkdayjobs.com/O_C_Tanner` ·
  BYU `byu.wd1.myworkdayjobs.com/byu-careers`

`companies.json` also holds a `needs_identification[]` backlog (eBay, Ivanti, Merit
Medical, Nu Skin, doTERRA, InMoment, Purple, Cotopaxi, Backcountry, FamilySearch, etc.) —
**not fetched**, just a to-do list.

## Lane 2 — US-Remote (`remote_companies.json`, 71 companies)

40 Greenhouse, 28 Ashby, 2 Lever, 1 Phenom.

- **Greenhouse:** GitLab, Cloudflare, Coinbase, Reddit, Datadog, Airtable, Twilio,
  Vercel, Grafana Labs, Postman, Webflow, Mercury, Netlify, Doximity, Gusto, Affirm,
  Discord, Stripe, Instacart, Fivetran, Hightouch, Temporal (`temporaltechnologies`),
  Dropbox, Calendly, Chime, Postscript, Cockroach Labs, Sourcegraph (`sourcegraph91`),
  Tailscale, LaunchDarkly, Amplitude, Mixpanel, ClickHouse, Chainguard, Cresta, Gemini,
  Together AI, AssemblyAI, Customer.io, Knock
- **Ashby:** Supabase, PostHog, Buffer, Linear, Replit, Ramp, Vanta, Render, Sentry,
  Zapier, Notion, Plaid, 1Password, Confluent, Docker, Airbyte, Close, Pinecone, Miro,
  Baseten, Deepgram, LangChain, Sanity, Alchemy, Sardine, Vantage, Doppler, Inngest
- **Lever:** Toptal, Metabase
- **Phenom:** Circle (`POST careers.circle.com/widgets`; `applyUrl`s point at Workday)

## Lane 3 — Contract / Staffing (`staffing_companies.json`, 3 firms)

- **Aquent** — RSS, `https://aquent.com/feeds/jobs.xml` (one national feed; no per-company slug)
- **Eliassen** — SnapHop Atom, `https://careers.eliassen.com/feeds/jobs.atom`
- **Addison Group** — SnapHop Atom, `https://careers.addisongroup.com/feeds/jobs.atom`

SnapHop feeds return only the **~100 most recent** roles and have **no pagination**.

## Adjacent, non-daily sources

- **`discover.py`** — separate weekly workflow (`.github/workflows/discover.yml`, Mondays
  14:00 UTC). Queries the **Obra/Silicon Slopes job board's Typesense index**
  (`https://2548bdkc7if30qglp.a1.typesense.net/collections/jobs/documents/search`, embedded
  search-only key, geo `40.5622,-111.9297, 50 mi`) to facet employer names, then probes up
  to 80 unknown names against Greenhouse / Lever / Ashby / SmartRecruiters and writes
  suggestions to `discovered.md`. Never modifies `companies.json`, never calls Anthropic.
- **`fetch_logos.py`** — occasional manual prefetch of `logos/<slug>.png` via logo.dev
  (`LOGO_DEV_TOKEN`). Not run by the daily job.

---

# Search Rules

Order of operations per run: **fetch → global location gate → global age gate → per-profile
title filter → salary enrichment → LLM fit scoring → diff**.

## Location filters

Constants at the top of `jobmonitor.py`. All matching is `\bword\b` regex
(word-boundary aware, deliberately **not** substring — so `ut` doesn't match
"So**ut**hampton").

**`LOCAL_KEYWORDS`** (local lane + staffing lane):
`ut, utah, salt lake, south jordan, lehi, draper, sandy, provo, orem, american fork,
lindon, pleasant grove, cottonwood, midvale, murray, west jordan, riverton, bluffdale,
herriman`

**`is_local(loc)`** — keeps a role if:
1. `KEEP_REMOTE = True` and the location contains "remote", **and** either
   `allow_international_remote` is true or the location names no `INTERNATIONAL_MARKERS`
   token; **or**
2. the location matches any `LOCAL_KEYWORDS` token.

So an international remote role is kept only if it *also* names a local city.
`LOCAL_ONLY = True` means the gate is always applied.

**`is_us_remote(loc)`** (US-Remote lane only) — requires a remote marker
(`remote, anywhere, distributed, work from home, wfh, virtual`), then:
- names `us / usa / united states / u.s / u.s. / americas / north america` → keep
- names an `INTERNATIONAL_MARKERS` token → keep only if `allow_international_remote`
- names no country at all (bare "Remote"/"Anywhere") → **keep** (treated as US-eligible)

Location-locked roles (no remote marker) are dropped from this lane.

**`INTERNATIONAL_MARKERS`** (~70 tokens): united kingdom, uk, england, scotland, wales,
ireland, britain, canada, brazil, argentina, chile, colombia, peru, india, pakistan,
bangladesh, sri lanka, philippines, china, hong kong, japan, korea, singapore, malaysia,
indonesia, vietnam, thailand, taiwan, australia, new zealand, germany, france, spain,
portugal, italy, netherlands, belgium, poland, romania, bulgaria, ukraine, czech, hungary,
sweden, norway, denmark, finland, switzerland, austria, greece, turkey, serbia, croatia,
lithuania, estonia, latvia, slovakia, slovenia, israel, egypt, south africa, nigeria,
kenya, morocco, uae, united arab emirates, dubai, abu dhabi, saudi arabia, qatar, emea,
apac, latam, europe, asia pacific.
Names colliding with US places are **intentionally omitted**: Georgia, Mexico
(→ New Mexico), Jordan (→ South Jordan).

Per-fetcher location normalization worth knowing: Workday multi-location hits
("N Locations") are relabeled `"<search_text> (+N more)"`; SmartRecruiters/Ashby/Recruitee
append `"(Remote)"` when their remote flag is set; Aquent folds `remotetype` into the
location string; Phenom appends `"(Remote)"` **only** when the `applyUrl` path says remote
(its location field never says remote); SnapHop derives location heuristically from the
URL slug and summary text (`_snaphop_location`).

## Date / posting-age filters

- **`settings.max_posting_age_days` = 7** — applied in `collect_pool` via `_within_age` to
  the local and remote lanes. Drops postings whose `posted` date is more than 7 days old.
- **`settings.staffing_search.max_age_days` = 30** — overrides the above for the staffing
  lane only.
- `0`/`null` = keep all ages. **A posting with an unknown or unparseable date is always
  kept.**
- **`settings.star_within_days` = 1** — postings newer than this get a ⭐ in the report
  (display only, no filtering).
- `posted` sources: Greenhouse `first_published` (fallback `updated_at`) · Lever
  `createdAt` (epoch ms) · SmartRecruiters `releasedDate` · Workday `postedOn` relative
  text parsed approximately by `_workday_date` ("today", "yesterday", "N days", "N months") ·
  Ashby `publishedAt` · Personio `createdAt` · Aquent RFC-822 `pubDate` · SnapHop
  `published`/`updated` · Phenom `postedDate`/`dateCreated`.
- Consequence documented in the code: a role that ages out of the window leaves the pool
  and therefore appears as **removed** in the next diff.

## Employment types

- No employment-type filter exists in the local or US-Remote lanes — full-time,
  part-time, and contract postings are all eligible if the title matches.
- The staffing lane is contract-scoped **in the fetcher, on structured data, not the
  title**:
  - `fetch_aquent` keeps a posting only if `placement_type` contains one of
    `_CONTRACT_PLACEMENT` = `temporary, temp, contract, freelance, interim, c2h`
    (so "Temp to Perm" keeps; "Permanent"/"Direct Hire" drop). An empty/unknown
    `placement_type` keeps.
  - `fetch_snaphop` is **keep-unless-permanent**: drops an entry whose title+summary
    contains `_PERMANENT_MARKERS` = `permanent, direct hire, direct-hire, perm placement`
    unless the text also contains "contract".
- `fetch_aquent` additionally drops non-US roles by the structured ISO `country` code
  (anything not `US`/`USA`, honoring `allow_international_remote`), because the string
  gate's markers are full names and miss 2-letter codes.

## Title filters (per profile, `matches_profile`)

Title is lowercased, then: **rejected** if it matches ANY `exclude_any` term; **kept**
only if it matches **at least one term in EVERY** `match_groups` entry (AND across groups,
OR within a group). Every term is matched as `\bterm\b`, so `coo` will not match
"coordinator" and `vp` will not match inside a longer word. An empty `match_groups`
keeps all roles that passed the location/age gates. **Only the title is filtered — the
description is never keyword-matched.**

### Profile `chad` — "Chad — Software / Frontend / Microservices"
enabled · `background_file: chad_background.json` · `fit_mode: rank` · `remote_search: true` · no staffing lane

**match_groups** — one group (any one term must appear):
`engineer, developer, software, frontend, front-end, full stack, fullstack, backend,
back-end, microservice, microservices, platform, devops, web, ui`

**exclude_any:** `sales, account executive, recruiter, intern, support specialist, qa,
manager, customer`
*(note: `"manager, customer"` is a single literal term, not two)*

### Profile `lisa` — "Lisa — Director / C-level Operations & Customer Relations"
enabled · `background_file: lisa_background.json` · `fit_mode: rank` · `remote_search: true` · `staffing_search: true`

**match_groups — Group 1 (seniority; one must appear):**
`director, director of, executive director, senior director, sr. director, vp,
vice president, head of, chief, chief of staff, coo, cco, cxo, c-level, c-suite,
general manager, senior manager, sr. manager, manager, lead, principal, practice leader,
practice director, portfolio leader, program director, transformation, transformation lead,
organizational effectiveness, organizational development, strategy, strategic operations,
business transformation`

**match_groups — Group 2 (function/domain; one must appear):**
`operations, operating, business operations, strategy, strategy & operations,
strategy and operations, people operations, m&a, m&a integration, post-merger integration,
acquisition integration, integration management office, mergers, acquisitions, strategic,
strategic initiatives, organizational capability, operating model,
enterprise transformation, ai enablement, seo, customer, client, customer success,
customer experience, customer relations, customer operations, client services,
professional services, service delivery, delivery, implementation, consulting, post sales,
post-sales, member experience, experience, support, service, revenue operations, revops,
community, account management, people, employee experience, engagement, onboarding,
retention, renewals, success, care, advocacy, organizational development,
organizational effectiveness, change management, organizational change, change manager,
transformation, business transformation, operational excellence, operational efficiency,
ai adoption, ai transformation, chief of staff, program management, portfolio management,
leadership development, AI, artificial intelligence, enablement, adoption, automation,
digital transformation, future of work, innovation`

**exclude_any:** `engineer, engineering, developer, software, data scientist, technician,
intern, account executive, sales development, sdr, bdr, coordinator`

Notable: `strategy`, `transformation`, `business transformation`,
`organizational development`, `organizational effectiveness`, and `chief of staff` appear
in **both** groups, so a title containing one of them can satisfy both groups by itself
(e.g. "Transformation" or "Strategy" alone passes). Group 2 also contains very broad
single words — `experience`, `support`, `service`, `delivery`, `people`, `care`,
`success`, `client`, `customer`, `community`, `innovation`, `AI` — which is the main
source of loose matches.

---

# Scoring

Optional LLM relevance layer (`score_fit` / `enrich_with_fit`). It **ranks or filters**
roles that already passed the keyword filter; it never adds roles back.

## Activation

All three must hold, otherwise the feature no-ops silently and the run behaves as if
scoring didn't exist:
1. `settings.json` → `fit_scoring_enabled: true` (currently **true**) — master off-switch,
2. `ANTHROPIC_API_KEY` present in the environment (GitHub Actions secret) **and** the
   `anthropic` SDK installed (workflow does `pip install anthropic`),
3. the profile declares a `background_file` (both profiles do).

## Model and payload

- `FIT_MODEL = "claude-sonnet-5"` (constant in `jobmonitor.py`; the comment notes
  `claude-haiku-4-5-20251001` as a cheaper swap).
- `max_tokens = 400`. Single user message, no system prompt, no tools, no streaming,
  no prompt caching, one API call per scored posting.
- `DESC_LIMIT = 2000` — job descriptions are truncated to 2,000 characters (both at fetch
  time and again in the prompt).
- The **entire background JSON** (`lisa_background.json` ≈ 7.9 KB / 131 lines;
  `chad_background.json` ≈ 5.7 KB / 170 lines) is serialized with `json.dumps(indent=2)`
  and embedded in **every** call.

## The complete scoring prompt

Exactly as constructed in `score_fit` (`jobmonitor.py:655-666`). `{...}` marks runtime
interpolation; everything else is literal, including line breaks.

```
You screen job postings for one specific candidate. Decide whether this role is worth the candidate's attention. Be realistic: reward strong matches on seniority, function, and domain; penalize clear mismatches. Many strong-fit roles are poorly or generically titled, so weight the actual mandate, scope, and problems described in the posting over title keywords.

CANDIDATE:
{json.dumps(candidate, indent=2)}

JOB POSTING:
Title: {posting['title']}
Company: {posting['company']}
Location: {posting['location']}
Description: {desc[:2000]}

Respond with ONLY a JSON object, no prose and no markdown fences:
{"fit": "yes" | "maybe" | "no", "score": <integer 0-100>, "reason": "<20 words max>"}
```

Notes on the interpolated pieces:
- `candidate` = the whole parsed `background_file` object.
- `desc` = `posting["description"]`, or the literal fallback string
  `"(no description available — judge from title and location)"` when the source gave no
  description. **SmartRecruiters and Workday postings are title-only at list time, so those
  roles are always scored from title + location alone.** Phenom supplies only a short
  `descriptionTeaser`.
- Note the source line breaks: the four paragraph-level `\n\n` separators shown above are
  literal, so the prompt is one block of instructions, the candidate JSON, the posting, and
  the output contract.

## What determines yes / maybe / no

The model decides. The code does **not** derive `fit` from `score`, and does not enforce
any threshold — `fit` and `score` are two independent fields returned by the model, and a
mismatch between them is never reconciled. The only guidance the model gets is the prompt
above plus whatever the background file says. Post-processing in `score_fit`:

- `fit` is lowercased; **anything not in `{yes, maybe, no}` becomes `maybe`**.
- `score` = `int(r["score"])`, defaulting to `50` if the key is absent.
- `reason` is truncated to 200 characters (the prompt asks for ≤20 words).
- The reply is parsed by pulling the first `\{.*\}` match out of the text, so fences or
  stray prose still parse.
- **Any exception at all** (API error, bad JSON, non-int score) returns the neutral
  `{"fit": "maybe", "score": -1, "reason": "(scoring unavailable: <ExceptionName>)"}`,
  logs the raw reply, and **keeps the role**. A run never crashes on scoring.

Steering that shapes the verdicts lives in the background files, e.g.
`lisa_background.json` includes `seniority_flexibility` ("GENUINELY OPEN to Manager and
Senior Manager… only apply a seniority penalty when the role is individual-contributor or
non-leadership"), `title_caveat` ("best-fit roles are FREQUENTLY POORLY OR GENERICALLY
TITLED — judge from the mandate"), `problem_signals` (M&A integration / AI adoption /
enterprise transformation / scaling-company vocabulary), `must_haves`, `nice_to_haves`, and
`dealbreakers` (IC-only, non-leadership, sales-quota, hands-on engineering).

## How the verdict is used

- `fit_mode: "rank"` (both profiles today) — nothing is dropped; the report is sorted by
  `score` descending.
- `fit_mode: "filter"` — roles whose `fit == "no"` are removed from `matched` **before the
  diff**, so a `"no"` role never enters the snapshot and never appears anywhere in the report.

## Caching (this is the cost control)

`enrich_with_fit` scores **only roles not already in the previous snapshot with a valid,
current verdict**. A cached verdict is reused only when both hold:
1. `fit_result.score >= 0` — failures (`score -1`) are never cached, so a transient error
   retries next run;
2. `fit_result.bg == _bg_fingerprint(candidate)` — a 12-char SHA-256 prefix of the
   background JSON.

Editing a `background_file` changes the fingerprint and therefore **auto-invalidates every
cached verdict for that profile** (full re-score next run). Legacy verdicts with no `bg`
also re-score once. Daily cost is proportional to *new* postings, not total tracked roles.

---

# Email

## Generation

`run_profile` builds a list of lane dicts (`{title, matched, new, removed, changed, errors,
first_run}`) and passes it to both renderers:

- `build_report` → `report_<name>.md` (Markdown, `_md_lane` per lane) — committed for
  history, not emailed.
- `build_html_report` → `report_<name>.html` — **this is the email body.**

`main` then writes two per-profile values to `$GITHUB_OUTPUT`:
`<name>_changed` (true/false) and `<name>_logos` (comma-separated list of the exact logo
files that report referenced). The workflow's email step for each person is gated on
`<name>_changed == 'true' || inputs.force_email`, sends `html_body: file://report_<name>.html`
via `dawidd6/action-send-mail@v18` over Gmail SMTP (465/SSL), and passes `<name>_logos` as
`attachments:`. `<name>_changed` is the **OR across that person's lanes** (so a
removal-only day still emails); `<name>_logos` is the **union**. First run counts as
changed.

Subjects are hardcoded in the workflow: Chad — "Prospector — your daily jobs report";
Lisa — "Prospector — director / ops roles today". Recipients are secrets
(`MAIL_TO_CHAD`, `MAIL_TO_LISA`).

## HTML structure (in order)

1. **Hidden preheader** (`_preheader` / `_summary_text`) — invisible first-in-body text
   that controls the inbox snippet, e.g. `"5 new roles · 🌎 3 US-Remote  📍 2 Local · Top:
   <title> @ <company> (87)"`, followed by invisible padding so the client can't append
   body text.
2. **Header** — profile `label`, then `"Job report · <date>"` + `" · ranked by fit"` (when
   scored) + `" · ⭐ = posted in the last N days"`.
3. **Digest hero** (`_digest_hero`) — headline count, per-lane new breakdown, changed/
   removed tallies, a row of up to 8 company logo tiles (34px) for the new roles' companies
   with a `+N` overflow, then the **Top pick**.
4. **Each lane** (`_html_lane`), in display order **🌎 US-Remote → 🧑‍💼 Contract/Staffing →
   📍 Local** (note: run order is Local first; display order is reversed). Per lane, with a
   `_lane_banner` header when more than one lane exists:
   - **"What's changed"** — a green `New · N` chip with one card per new role, then an amber
     `Changed titles · N` chip with `"was <old title>"` cards. If `first_run`: "First run —
     baseline established." If nothing new/changed: "No new or changed roles since the
     previous run."
   - **"All current matching roles (N)"** — every currently-matching role.
   - **"Source warnings (N)"** — any fetch exception, as `"<Company> (<ats>/<slug>):
     <ExceptionName> <msg>"`.
5. **"Removed / filled"** — pulled out of *every* lane and collected in ONE region at the
   very bottom, with a red per-lane sub-header chip.
6. Footer: "Prospector · Silicon Slopes job monitor".

Styling is inline-only, table-based, fixed GitHub-dark palette (`_C`), 780px max width,
Inter with a system-sans fallback. Each role row shows: ⭐ (if fresh) · linked title ·
colored fit pill (`<score>/100 · <fit>`, green=yes / amber=maybe / red=no; hidden when
score < 0) · muted "company · location · Posted Jul 10 · 3d ago" · salary in green ·
the model's `reason` in muted text.

**Logos:** a 40–64px tile left of each posting. Uses the prefetched `logos/<slug>.png`
embedded **inline via CID** (`<img src="cid:<slug>.png">`) — which works because
`dawidd6/action-send-mail@v18` sets each attachment's Content-ID to its filename. Missing
file → a colored monogram of the company's initials (`_mono_color`/`_initials`).
`_LOGOS_USED` (cleared at the top of `build_html_report`) records which files the report
actually referenced so only those are attached. Slugs are resolved from all three
registries. Data-URI/base64 logos are explicitly avoided (Gmail/Outlook strip `data:`).

## Sorting

Within each lane, `scored = any posting has a fit_result`:

| Section | Scored | Not scored |
|---|---|---|
| New | `score` descending (`-score`, missing → 0) | by company name |
| Changed titles | dict/iteration order from `diff` (no explicit sort) | same |
| All current matching roles | `score` descending, flat list | grouped by company, then `(company, title)` |
| Removed / filled | by company name | by company name |

Lanes themselves are not sorted by anything — the display order is hardcoded.

## Top Pick

In `_digest_hero`, only when there is at least one new role anywhere:
`_new_companies_by_fit(all_new, scored)` sorts **all new roles across all lanes** by
`-fit_result.score` (missing score → 0) and `order[0]` is the Top pick. If nothing is
scored, it falls back to alphabetical-by-company, so the "Top pick" is then just the
first company alphabetically. It is rendered as star + linked title + fit pill + company.
There is no tie-break, no lane preference, and no minimum score — the highest-scoring
**new** role wins regardless of lane. (Note: the hidden preheader picks its "Top:" using
`max()` over the same set, which can differ from `order[0]` in a tie.)

## Why jobs repeat

Two distinct behaviors:

1. **By design, within one email:** every report has both "What's changed" (new + changed
   only) *and* "All current matching roles" — so a new role appears **twice in the same
   email**, once as a New card and once in the full list.
2. **Day over day:** the "All current matching roles" section is a **full standing
   inventory**, re-rendered every run for every role still live and still matching. A role
   therefore reappears in that section every single day until it disappears from its
   source feed or ages past the age window. Only "What's changed" is diff-limited. And
   because the email is gated on `<name>_changed` (any new/removed/changed in **any** lane),
   one new role in one lane re-sends the entire standing inventory for all lanes.

Mechanisms that *prevent* extra repeats: the diff identity `key = "<Company>::<ats_id>"` is
stable, so an unchanged role is never re-reported as new; fit verdicts are cached, so a
repeated role is not re-scored. A role can legitimately re-enter "New" if its `key` changes
(company renamed in the registry, ATS migration, or the company reposting with a new req
id), or if it ages out of the window and later returns.

## Removed jobs

`removed` = keys in the previous snapshot that are **not** in the current matched set.
The engine cannot distinguish *filled* from *pulled*, *aged out of the 7-day window*,
*retitled such that it no longer matches*, *newly excluded by an edited filter*, or *a
source that errored this run* — all of these surface identically as "removed".

Handling: removals are stripped out of every lane's "What's changed" and collected in a
single **"Removed / filled"** region at the very bottom of the report, one red-chipped
sub-section per lane, sorted by company, showing title + `company · location · posted` (no
link styling, no fit pill). Removals **do** count toward `<name>_changed`, so a
removal-only day still sends an email. Once removed, a role is gone from the snapshot and
is never mentioned again on subsequent runs.

---

# State

Everything Prospector remembers lives in JSON files committed back to the repo by CI
(`git add snapshot_*.json report_*.md report_*.html`). There is no database and no cache
outside these files.

**Snapshots — the only true state.** One per profile per lane:
`snapshot_chad.json`, `snapshot_chad_remote.json`, `snapshot_lisa.json`,
`snapshot_lisa_remote.json`, `snapshot_lisa_staffing.json`.

Written at the end of each lane run as the **full current matched set** (a slim copy of
`matched` with `description`, `_ats`, and `_detail_url` stripped). Each entry:

```json
{
 "key": "Podium::8080202",
 "company": "Podium",
 "title": "Associate Manager, Onboarding",
 "location": "Lehi, Utah",
 "url": "https://job-boards.greenhouse.io/podium81/jobs/8080202",
 "posted": "2026-07-22",
 "salary": null,
 "fit_result": {
  "fit": "no",
  "score": 20,
  "reason": "Junior front-line onboarding team lead role; below seniority target and lacks transformation/strategy mandate.",
  "bg": "e375d8ec09f3"
 }
}
```

So a snapshot carries **two things at once**: the diff baseline (`key` + `title` are all
`diff` compares) and the **fit-verdict cache** (`fit_result`, each stamped with the `bg`
fingerprint it was scored against). A missing snapshot file = first run: no diff, nothing
marked new/removed, `changed = true`, and every role gets scored. Wiping a snapshot forces
a full re-score (a full API bill); the sanctioned way to force a re-score is to edit the
`background_file`, which invalidates verdicts via the fingerprint.

**Also persisted but not read back as state:** `report_<name>.md` / `report_<name>.html`
(overwritten each run, committed for history), `logos/<slug>.png` (prefetched, committed),
`discovered.md` (weekly discovery output).

**In-memory only, per run:** `_SALARY_CACHE` (key → salary, so a role matched by multiple
profiles is fetched once), `_LOGOS_USED`, `_COMPANIES`, and the fetched pools.

---

# Configuration

| Concern | File(s) | Notes |
|---|---|---|
| **Search sources** | `companies.json` (local, 38) · `remote_companies.json` (US-remote, 71) · `staffing_companies.json` (contract, 3) | Entry shape `{name, city, ats, slug, domain}`; Workday also needs `wd_host`, `site`, optional `search_text`; SnapHop/Phenom use `domain` (+ optional `feed_url` / `phenom_host`). `domain` also feeds logo prefetch. `companies.json` additionally holds the `needs_identification[]` backlog. Adding an ATS requires a new `fetch_*` + a `FETCHERS` entry in `jobmonitor.py`. |
| **Search rules — titles/keywords** | `profiles.json` | `match_groups` (AND across groups, OR within) and per-profile `enabled`, `label`, `remote_search`, `staffing_search`, `fit_mode`, `background_file`. |
| **Search rules — location** | `jobmonitor.py` constants | `LOCAL_KEYWORDS`, `KEEP_REMOTE`, `LOCAL_ONLY`, `INTERNATIONAL_MARKERS`, `is_local`, `is_us_remote` — **code, not config**. `settings.allow_international_remote` (false) is the only location knob in config. |
| **Search rules — age / lanes** | `settings.json` | `max_posting_age_days` (7), `staffing_search.max_age_days` (30), `star_within_days` (1), `allow_international_remote` (false), `remote_search.enabled` (true), `staffing_search.enabled` (true). Defaults in `SETTINGS_DEFAULTS` (90 / true / 7 / false) apply if the file or a key is missing. |
| **Search rules — employment type** | `jobmonitor.py` constants | `_CONTRACT_PLACEMENT` (Aquent) and `_PERMANENT_MARKERS` (SnapHop) — code, not config. |
| **Scoring — on/off** | `settings.json` `fit_scoring_enabled` + `ANTHROPIC_API_KEY` env (GitHub secret) | Either one off ⇒ no API calls. |
| **Scoring — candidate criteria** | `chad_background.json`, `lisa_background.json` (named by each profile's `background_file`) | The entire file is sent to the model per posting; editing it auto-invalidates that profile's cached verdicts. |
| **Scoring — prompt, model, verdict handling** | `jobmonitor.py` | `score_fit` (prompt text), `FIT_MODEL`, `DESC_LIMIT`, `max_tokens=400`, `enrich_with_fit` (caching), `_bg_fingerprint`. Not configurable from JSON. |
| **Scoring — rank vs. drop** | `profiles.json` `fit_mode` | `"rank"` (both profiles) or `"filter"`. |
| **Email layout** | `jobmonitor.py` | `build_html_report`, `_digest_hero`, `_preheader`, `_summary_text`, `_html_lane`, `_card`, `_chip`, `_section`, `_role_inner`, `_meta_html`, `_fit_pill_html`, `_logo_square`/`_logo_tile`/`_icon_row`, palette `_C`, font `_FONT`. Markdown twin: `build_report` / `_md_lane`. |
| **Email delivery** | `.github/workflows/prospector.yml` | Schedule, `pip install anthropic`, snapshot commit, per-person email steps, subjects, `changed` gating, logo attachments. Recipients/creds are secrets: `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_TO_CHAD`, `MAIL_TO_LISA`, `ANTHROPIC_API_KEY`. Manual-dispatch inputs: `chad_only`, `force_email`. |
| **Exclusions — titles** | `profiles.json` `exclude_any` | Per profile, word-boundary matched against the title only. |
| **Exclusions — geography** | `jobmonitor.py` `INTERNATIONAL_MARKERS` + `settings.allow_international_remote`; `fetch_aquent`'s ISO-country check | |
| **Exclusions — age** | `settings.json` age keys | |
| **Exclusions — employment type** | `_CONTRACT_PLACEMENT` / `_PERMANENT_MARKERS` in `jobmonitor.py` (staffing lane only) | |
| **Exclusions — LLM verdict** | `profiles.json` `fit_mode: "filter"` (drops `fit == "no"`) — not currently used | |
| **Exclusions — discovery** | `discover.py` `DENYLIST` (staffing/aggregator names) | Affects `discovered.md` suggestions only. |
| **Logos** | `fetch_logos.py` + `logos/` + `LOGO_DEV_TOKEN` | Manual/occasional; not part of the daily run. |

Docs: `CLAUDE.md` (architecture + hard-won ATS notes), `README.md` (narrative + Actions
setup), `DESIGN-remote.md` (US-remote lane design and why a feed-based approach was
dropped).

---

# Efficiency

**Where Claude tokens go.** There is exactly one kind of API call in the system: one
`messages.create` per **newly scored posting** (`score_fit`, `claude-sonnet-5`,
`max_tokens=400`). Cached verdicts cost nothing. Token spend is therefore
`new_postings_today × per-call size`, and the per-call input is dominated by two things:

1. **The candidate background JSON — the single largest and most repeated cost.** The whole
   `background_file` is re-serialized into **every** call: `lisa_background.json` ≈ 7.9 KB
   (~2,000+ tokens), `chad_background.json` ≈ 5.7 KB. It is identical across every call in
   a run, and there is **no prompt caching** — so with N new roles it is paid N times. For
   Lisa's lanes this is typically the majority of input tokens per call.
2. **The job description**, capped at `DESC_LIMIT = 2000` characters (~500 tokens). Present
   for Greenhouse, Lever, Ashby, Recruitee, Personio, Aquent, SnapHop; **absent** for
   SmartRecruiters and Workday (title-only, so those calls are cheaper), and only a short
   teaser for Phenom.

The fixed instruction text and the title/company/location lines are negligible by
comparison. Output is capped at 400 tokens and the reply is a small JSON object
(`reason` ≤ 20 words), so **input dominates output** by roughly an order of magnitude.

**Amplifiers of the per-run count:**
- Scoring runs **per profile per lane** — Lisa has 3 lanes, Chad has 2. The same posting
  matched by two profiles is scored **twice** (verdict caching is keyed inside each
  profile's own snapshot; there is no cross-profile verdict sharing — unlike
  `_SALARY_CACHE`, which *is* shared).
- Scoring happens **after** the keyword filter, so the loose Group-2 terms in Lisa's
  profile (`experience`, `support`, `service`, `people`, `delivery`, `success`, `AI`, …)
  directly increase how many roles reach the model.
- The 7-day age window bounds the pool, which bounds new postings.
- The `remote_companies.json` registry (71 companies) is the largest source of new
  postings per day and thus the largest scoring driver.
- Any scoring failure returns `score -1` and is deliberately **not cached**, so failing
  postings are re-scored every run until they succeed.

**Non-Claude network cost** (no tokens, but the other bounded expense): one list-feed call
per company per run (Workday/Phenom/SmartRecruiters paginate; Adobe uses `search_text` to
avoid paging ~897 roles), plus at most one **detail** fetch per matched role missing a
salary — cached by `key` in `_SALARY_CACHE` and bounded to matched roles only.
