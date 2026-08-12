# Prospector V3 — broaden discovery, track lifecycle, report change

What changed, why, and what you need to do. V3 is an **evolution of the existing engine**, not
a rewrite: the tiered ATS fetchers, the location gates, the lane machinery, the fit-scoring
cache and Chad's email are all untouched.

---

## The five reported problems, and what fixes each

| # | Problem | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | Too few genuinely new jobs | `match_groups` is a **precision** filter that was doing a **recall** job. Its two groups are ANDed, so a role had to name both a seniority word and a function word *in its title* to be retrieved at all. On one measured day that discarded **130 of 171** leadership-shaped roles for "missed match group [2]" — before the model ever saw them. | The **discovery gate** (`classify_match`) runs after the precise gate: a role that misses it is still retrieved if its title names a role family *and* it carries either a seniority marker or real mandate language in its description. Plus 4 new sources. |
| 2 | >2-week-old jobs kept reappearing | Lisa's display window was **30 days** and ranking was score-first, so 65 of 113 active roles were 15–30 days old and outranked fresher ones. | **Explicit lifecycle bands.** 0–7 new, 8–14 still-worth-applying, 15+ suppressed unless the role scores ≥ `exceptional_score` **and** was independently verified still open on that run. |
| 3 | "Removed" jobs that were never in an email | The snapshot was the active inventory, but the digest showed ~18 of ~113. A role could enter and leave the inventory without ever being rendered, then surface as removed. Nothing tracked whether a role had been **shown**. | `ever_shown`, set **only** by `lifecycle.mark_shown` on the roles the rendered email actually contained. The removed section reads from it. |
| 4 | Error vs zero-results ambiguity | Every exception was flattened into one list of strings. A dead slug and a 5-second timeout looked identical, and `consecutive_zero_runs` counted both. | Four explicit states — `ok_results`, `ok_zero`, `temp_error`, `config_error` — with one retry for temporary failures, and a zero-streak that **only advances when the source actually answered**. |
| 5 | Static inventory feel | The digest ranked `lane["matched"]` — the whole active set — every morning. The computed diff was used only to decide whether to send email. | The **change digest**, built from the lifecycle DB, in the requested section order. |

---

## New files

| File | What it is |
|---|---|
| `lifecycle.py` | The persistent discovery database: one record per **real job**, cross-feed dedup, age bands, lifecycle states, bounded still-open verification. |
| `sheets_sync.py` | Discovery Log export — CSV always, Google Sheet when configured. |
| `jobs_<profile>.json` | **State.** The database itself. Committed by CI. Carries `first_seen`, `ever_shown`, and human decisions. |
| `discovery_log_<profile>.csv` | The same records, flat, for import or inspection. |

---

## A. Discovery separated from scoring

Three outcomes per posting, per profile:

- **core** — the historical `match_groups` gate, unchanged. High confidence.
- **discovery** — a role family in the title **plus** seniority in the title *or* ≥2 distinct
  mandate terms in the description.
- **dropped** — hard-excluded (wrong function outright), or no signal at all.

The hard-exclude list (`discovery.exclude_any` in `profiles.json`) is deliberately **much
shorter** than the precision `exclude_any`: every term in it is a function Lisa can never
want, because each one is a potential false negative.

Measured on a live pool, retrieval went from **113 → 254 roles** (86 core + 124 discovery +
44 from new sources), from 1,485 fetched postings.

**Known limit, stated plainly:** SmartRecruiters and Workday give no description at list
time, so a *generically titled* role at one of those sources still cannot be rescued — its
title has to name a role family. `Sr. Product Manager` and `Senior Director, Revenue
Analytics` are still dropped, because neither names a family in the requested list. Widening
further means scoring titles with no relevance signal at all.

### Cost control
`settings.discovery.max_new_scored_per_run` (default 150) caps NEW roles sent to Claude per
run. Core tier is paid for first. Roles over the cap are **kept and marked `score_pending`**,
never dropped, and are scored on the next run.

---

## B. Sources

**Added** (all verified live 2026-08-11):

| Source | Type | Surface |
|---|---|---|
| **Robert Half** | staffing lane | `/us/en/jobs` server-renders its full result set into `aemSettings.rh_job_search.initialResults`. Richer than most ATS feeds: full description, real `date_posted`, employment type, structured pay range, remote flag, canonical apply URL, stable job number. |
| **Himalayas** | remote lane | `GET himalayas.app/jobs/api?limit&offset` — 100k jobs, seniority/salary/category/expiry fields. |
| **We Work Remotely** | remote lane | Category RSS, management-and-finance / product / customer-support only. |
| **Jobicy** | remote lane | `GET jobicy.com/api/v2/remote-jobs` — only the `business`, `management`, `hr`, `marketing`, `project-management` industry slugs are accepted; the rest return HTTP 400. |

**Rejected, with reasons:**

- **Kforce** — no RSS, no sitemap, no ATS. Its Find Work SPA queries an Azure Cognitive
  Search index (`kforcewebeast.search.windows.net`) that returns **403 without the API key
  embedded in their client**, and `robots.txt` disallows `/svc` and `/kforcesvc`. There is no
  clean way to support it. **Recommend dropping it from the list.**
- **Robert Half's own JSON API** (`prd-dr.jps.api.roberthalfonline.com/search`) — 403 without
  a client credential. The public page is the supported surface.
- **Remotive** — its public feed returns only ~20 jobs.
- **LinkedIn** — deliberately not a dependency. Its guest endpoints block CI IPs.

The three aggregator rows differ from every other registry row in one way that matters: the
**employer varies per posting**, so the row is the *source*, not the company. Their fetchers
pass the real employer to `_norm` and record the board in `_source`.

---

## C/D. The lifecycle database

One record per real job, keyed by `normalized_company | normalized_title |
coarse_location`. That is what makes the same role on a company's Greenhouse board and on
Himalayas **one row** — while keeping "Director, Operations" in Lehi and in Austin as two
jobs, and "Manager" and "Senior Manager" as two jobs.

Canonical URL prefers **employer/ATS → staffing firm → aggregator**, so the same job found on
both keeps the company's real apply link.

Rules that matter:

- **A feed error never marks a job removed.** If every source a record was found through
  failed this run, the record is left completely alone and stamped `unverified_run` — we did
  not look, we merely failed to look.
- **A job may only appear in "Removed since prior run" if `ever_shown` is true.**
- 15+ day roles are suppressed **unless** score ≥ `exceptional_score` (85) **and**
  `verify_open` confirmed the URL is still live on that run. Verification is capped at
  `verify_limit` (15) requests per run and only spent on roles that could actually benefit.
- `verify_open` is conservative: only a 404/410 or an explicit "no longer available" on the
  page counts as closed. A timeout or a bot wall is `unknown`, and unknown never closes a role.

---

## E. Source health in the email

```
Sources checked: 129/129 successful · 127 returned roles · 2 returned none
Temporary errors: 1 (retried; roles from these sources were held, not marked removed)
Needs attention: 2 — Weave, Vivint
```

"Needs attention" = a `config_error`, or a source that has **answered with nothing** for
`STALE_SOURCE_RUNS` (10) runs running — the ATS-migration signal. An outage no longer
pollutes that streak.

---

## F/G. The change digest

`report_style: "change"` on the profile. Sections, in order: **Apply first → New — worth
reviewing → Discovery / wildcards → Still worth applying → Removed since prior run →
Source health.** Each card carries company, title, fit score, location + work arrangement,
compensation when known, posting date *or* first-seen date (labelled, so an undated posting is
never passed off as fresh), why it fits, the strongest concern, and a direct apply link.

Wildcards hold 2–4 roles the model scored honestly but with **low confidence**, or that came
in through the broad discovery net. The scoring prompt now says so explicitly: uncertainty is
preserved for a human, not resolved by discarding.

Chad's profile has no `report_style` and no `discovery` block — his retrieval and his email
are byte-for-byte what they were.

---

## H. Decisions

`feedback_<name>.json` statuses: `pursue`, `applied`, `already_applied`, `interested`,
`not_interested`, `wrong_function`, `too_technical`, `compensation`, `location`, `seniority`,
`industry_requirement`, `weak_fit`, `duplicate`, `closed`.

`pursue` and `interested` deliberately do **not** suppress — those say "I want this".
`compensation` and `location` suppress but are **not** counted as false positives by the
audit: they say something about the job, not about the match.

No learned weights. Every rule is explicit and every selection carries a `match_reason`.

---

## I. Google Sheet — SETUP REQUIRED

Prospector writes `discovery_log_<profile>.csv` on every run regardless. To have it write the
**Prospector Discovery Log** tab directly, deploy this Apps Script once.

There is no zero-dependency way to authenticate to the Sheets API from the standard library
(a service account needs RS256 JWT signing, which needs a crypto library). A Web App bound to
the sheet gives the same result over plain HTTPS: the script runs as **you**, so no credential
ever leaves Google and Prospector only needs the deployment URL.

### Setup, step by step

You do all of this in a **browser**, starting from the spreadsheet itself — not from Google
Drive, and not from script.google.com. Reaching the editor *from inside the sheet* is what
BINDS the script to that spreadsheet, and that binding is the whole trick: it is why the code
below can call `getActiveSpreadsheet()` with no API key, no OAuth client and no service
account anywhere.

> **Who does this:** any Google account with **edit** access to the tracking spreadsheet —
> it does not have to be the owner, but Viewer/Commenter is not enough (the
> `Extensions → Apps Script` menu only appears with edit rights).
>
> The script is bound to that sheet and runs as whichever account deploys it, so the account
> needs edit access for as long as the integration is meant to keep working. If the sheet
> belongs to someone else, the tidier arrangement is for THEM to run steps 1–8 and hand over
> only the resulting `/exec` URL: that is all Prospector needs, nothing has to be shared, and
> the daily push cannot break later because someone's access changed.
>
> "Execute as: **Me**" in step 6 is required, not a preference — GitHub Actions POSTs
> anonymously, so there is no signed-in user for the alternative setting to run as. On a
> Google Workspace account an admin policy can block web apps set to "Anyone"; if Deploy
> refuses, that is why, and deploying from a personal account that owns the sheet avoids it.
>
> This repo is PUBLIC, so the spreadsheet URL is deliberately not written down here (same
> rule as "no email addresses in the repo" — a doc link is an access-bearing credential).
> Keep it in the GitHub secret only.

1. **Open the tracking spreadsheet in a browser** — the sheet itself, not Google Drive and
   not script.google.com.
2. In the sheet's own menu bar, choose **Extensions → Apps Script**. A new browser tab opens
   with a code editor, already attached to this spreadsheet.
3. Delete the placeholder `function myFunction() {}` and paste the code below in its place.
   Save with **Cmd/Ctrl+S**.
4. Click the blue **Deploy** button (top right) → **New deployment**.
5. Click the **gear icon** next to "Select type" and choose **Web app**.
6. Set **Execute as: Me** and **Who has access: Anyone**, then **Deploy**.
7. Google will ask you to authorize it. Choose your account, then click through the
   *"Google hasn't verified this app"* warning via **Advanced → Go to … (unsafe) → Allow**.
   That warning is expected for your own unpublished script.
8. Copy the **Web app URL** (it ends in `/exec`).
9. In GitHub: repo → **Settings → Secrets and variables → Actions → New repository secret**.
   Name it exactly **`SHEETS_WEBHOOK_URL`** and paste the URL.

**On step 6:** "Anyone" means anyone holding that URL can POST to it. The URL is long and
unguessable and the script only ever writes the one tab, but treat it as a password — that is
the trade for not needing a service account. Redeploying with a new version keeps the same
URL; creating a *new deployment* mints a different one.

### Testing the webhook

Once `SHEETS_WEBHOOK_URL` is set as a repo secret, test it **from the Actions tab** —
no terminal, no local copy of the URL:

> GitHub → **Actions** → **prospector-sheets-check** → **Run workflow**

That workflow (`.github/workflows/sheets-check.yml`) exists purely so the webhook can be
verified on its own. It makes **exactly one HTTPS POST** and nothing else — no ATS requests,
no Anthropic calls, no email, nothing committed. It writes one obviously-labelled probe row
you can delete from the sheet afterwards. Tick *"Send the real discovery records"* to push
the actual database instead, once the daily job has run at least once.

To run the same check locally instead:

```bash
python3 check_sheets.py           # prompts for the URL, input hidden
python3 check_sheets.py --rows    # send the real records rather than a probe row
```

`status: ok` means it worked. If it fails, the script prints the three things that actually
cause it: a `/dev` URL instead of `/exec`, "Who has access" not set to **Anyone** (Google
returns a login page instead of running the script), or edits saved but never redeployed.

### The script

```javascript
function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(body.tab) || ss.insertSheet(body.tab);
  const cols = body.columns;
  const keyCol = cols.indexOf(body.key_column);

  // Header row, written once.
  if (sheet.getLastRow() === 0) sheet.appendRow(cols);

  // Index existing rows by job_key so a re-run UPDATES rather than duplicating.
  const existing = sheet.getLastRow() > 1
    ? sheet.getRange(2, keyCol + 1, sheet.getLastRow() - 1, 1).getValues().flat()
    : [];
  const rowFor = {};
  existing.forEach((k, i) => { rowFor[k] = i + 2; });

  const appends = [];
  body.rows.forEach(r => {
    const values = cols.map(c => (r[c] === undefined || r[c] === null) ? "" : r[c]);
    const key = r[body.key_column];
    if (rowFor[key]) {
      sheet.getRange(rowFor[key], 1, 1, cols.length).setValues([values]);
    } else {
      appends.push(values);
    }
  });
  if (appends.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, appends.length, cols.length)
         .setValues(appends);
  }
  return ContentService.createTextOutput(
    JSON.stringify({ ok: true, updated: body.rows.length - appends.length,
                     added: appends.length }))
    .setMimeType(ContentService.MimeType.JSON);
}
```

The script only ever touches the tab named in the payload, which Prospector hardcodes to
`Prospector Discovery Log`. **`Application Pipeline` and `Applied` are never written** —
promotion stays a human act, which is the point of keeping the three tabs separate.

If the webhook is unset or fails, the run logs it and continues. The sheet is a view;
`jobs_<profile>.json` is the system of record.

---

## Operating notes

- `python3 jobmonitor.py --dry-run --fake-fit` — full pipeline, no API cost, no email.
- Every run prints the discovery funnel (discovered / deduplicated / new / excluded /
  active / aged out / verified removed / feed errors).
- `python3 test_jobmonitor.py` — 223 offline tests, no network, no API.
- Tuning knobs live in `settings.json` under `lifecycle`, `discovery` and `sheets`.
