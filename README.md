# Prospector

> Prospector watches the careers pages of tech companies within ~30 miles of Salt Lake City and emails a daily report of new, changed, and removed job postings — one tailored report per person you're tracking for. It pulls structured data straight from each company's applicant tracking system, so there's no brittle web scraping.

Most tech companies don't hand-build their careers pages; they run them on an applicant tracking system (ATS) like Greenhouse, Lever, or SmartRecruiters, and those platforms expose clean JSON endpoints listing every open role. Prospector pans those streams once a day, filters them through one or more named **profiles**, compares today's haul against yesterday's, and reports only what changed.

## What it does

- Fetches current open roles from every company in your list via their ATS's JSON API — **once per run**, shared across all profiles.
- Normalizes Greenhouse, Lever, and SmartRecruiters into one common shape.
- Applies a global location gate (Utah + remote), then runs each **profile** — a named title filter — against the results.
- Diffs each profile against its own previous snapshot to surface **new**, **changed**, and **removed/filled** postings.
- Writes a separate snapshot and report per profile, so one search never contaminates another's history.

No headless browser, no HTML parsing for the current sources, no third-party dependencies. Python standard library only.

## How it works

Fetching is tiered, cheapest and most reliable first:

1. **ATS JSON API** (Greenhouse, Lever, SmartRecruiters) — structured, fast, stable. Covers most of Silicon Slopes.
2. **Plain HTTP + HTML parse** — for a simple static careers page with no API. (Not needed by the current list.)
3. **Headless browser (Playwright)** — last resort, only for pages that render entirely in JavaScript with no reachable data source.

The daily run: read the company list → fetch and normalize every source once → apply the location gate → for each profile, filter by title rules, diff against that profile's saved snapshot, and write its new snapshot and report.

## Repo layout

| File | Purpose |
|------|---------|
| `companies.json` | Where to look: each company's name, city, ATS, and slug. Plus a `needs_identification` backlog. |
| `profiles.json` | What to look for: one named title filter per person. |
| `jobmonitor.py` | The engine — fetch, normalize, filter, diff, report. |
| `snapshot_<name>.json` | Auto-generated per profile. The last run's matched roles, used for the diff. Commit these so history lives in git. |
| `report_<name>.md` | Auto-generated per profile. The latest report — this is what gets emailed. |

## Quick start

```bash
python3 jobmonitor.py                 # fetch once, run every enabled profile
python3 jobmonitor.py --profile lisa  # run just one profile
python3 jobmonitor.py --list          # show configured profiles
```

The first run for a profile establishes a baseline (no diff yet). Every run after reports what changed since the last.

## Configuring companies

Edit `companies.json`:

```json
{ "name": "Lucid Software", "city": "South Jordan", "ats": "greenhouse", "slug": "lucidsoftware" }
```

`ats` is one of `greenhouse`, `lever`, or `smartrecruiters`. `slug` is the company's identifier on that platform.

## Configuring profiles

Each profile in `profiles.json` is a title filter. **A role is kept when its title matches at least one term in _every_ `match_group` (AND across groups, OR within a group) and matches _none_ of `exclude_any`.** Matching is word-boundary aware, so `coo` matches "Chief Operating Officer" but not "Coordinator."

```json
{
  "name": "lisa",
  "label": "Lisa — Director / C-level Operations & Customer Relations",
  "enabled": true,
  "match_groups": [
    ["director", "vp", "vice president", "head of", "chief", "coo", "senior manager"],
    ["operations", "customer", "client", "support", "experience", "revops"]
  ],
  "exclude_any": ["engineer", "engineering", "developer", "software", "analyst", "intern"]
}
```

That profile keeps a role only if it's **both** senior (group 1) **and** in her domain (group 2). A single-group profile (like the `chad` engineering filter) behaves like a simple keyword include-list. Set `"enabled": false` to mute a profile without deleting it. All tuning lives here — no code changes.

## Location filtering

A coarse global gate runs once before any profile, configured at the top of `jobmonitor.py`:

- `LOCAL_KEYWORDS` — city/state terms that count as local.
- `KEEP_REMOTE` — when `True`, remote-tagged roles are kept regardless of geography.
- `LOCAL_ONLY` — set `False` to track every role regardless of location.

## Adding a company

1. Identify the ATS. A live endpoint returns JSON; a bad slug 404s on Greenhouse/Lever, but **SmartRecruiters returns an empty-but-valid response for any slug**, so confirm the role count is non-zero.
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs`
   - Lever: `https://api.lever.co/v0/postings/<slug>?mode=json`
   - SmartRecruiters: `https://api.smartrecruiters.com/v1/companies/<slug>/postings`
2. Add an entry to `companies.json`.
3. Run once to fold it into each profile's baseline.

Companies on Workday, iCIMS, or a custom stack (several of the larger Silicon Slopes names) need a per-company endpoint — that's the tier-2 work tracked in `needs_identification`.

## Running daily with GitHub Actions

Runs Prospector on a schedule, commits updated snapshots back, and emails each person their report — no server to maintain. Save as `.github/workflows/prospector.yml`:

```yaml
name: prospector
on:
  schedule:
    - cron: "0 13 * * *"   # 7:00 AM Mountain (MDT); use "0 14 * * *" to hold at 7 in winter
  workflow_dispatch:

permissions:
  contents: write

jobs:
  prospect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }

      - name: Run prospector (all profiles)
        run: python jobmonitor.py

      - name: Commit updated snapshots
        run: |
          git config user.name  "prospector-bot"
          git config user.email "actions@users.noreply.github.com"
          git add snapshot_*.json report_*.md
          git commit -m "Daily run: $(date -u +%Y-%m-%d)" || echo "No changes"
          git push

      - name: Email Chad's report
        uses: dawidd6/action-send-mail@v3
        with:
          server_address: smtp.gmail.com
          server_port: 465
          secure: true
          username: ${{ secrets.MAIL_USERNAME }}
          password: ${{ secrets.MAIL_PASSWORD }}
          from: Prospector Bot
          to: ${{ secrets.MAIL_TO_CHAD }}
          subject: "Prospector — your daily jobs report"
          body: file://report_chad.md

      - name: Email Lisa's report
        uses: dawidd6/action-send-mail@v3
        with:
          server_address: smtp.gmail.com
          server_port: 465
          secure: true
          username: ${{ secrets.MAIL_USERNAME }}
          password: ${{ secrets.MAIL_PASSWORD }}
          from: Prospector Bot
          to: ${{ secrets.MAIL_TO_LISA }}
          subject: "Prospector — director / ops roles today"
          body: file://report_lisa.md
```

### Credentials and recipients — you set these, not this repo

Never put a password or someone's email address in a committed file. In **Settings → Secrets and variables → Actions**, add:

- `MAIL_USERNAME` — the sending Gmail address.
- `MAIL_PASSWORD` — a Gmail **App Password** (created in your Google account's security settings; a normal password won't work with 2FA and shouldn't be used regardless).
- `MAIL_TO_CHAD`, `MAIL_TO_LISA` — recipient addresses (secrets keep Lisa's address out of the repo).

The workflow references these by name only, so nothing sensitive touches the code or git history.

Report bodies are Markdown, which most mail clients render as clean plain text. For rich formatting, generate an HTML version in `jobmonitor.py` and use `html_body:`.

## Roadmap

- **Relevance filter** — a scoring pass on each profile's survivors (a cheap keyword rank first, an optional LLM pass on the rest) so reports stay signal-heavy.
- **Tier-2 coverage** — pin down endpoints for the larger Workday-based employers in `needs_identification` (Adobe, Domo, Pluralsight, etc.).
- **Source health alerts** — flag when a company's endpoint suddenly returns zero, which usually means an ATS migration and a stale slug.

## Notes

Everything here reads public careers-page data through the same endpoints the companies' own job boards use. One run a day is plenty and stays well within polite rate limits.
