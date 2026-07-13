# Prospector

> Prospector watches the careers pages of tech companies within ~30 miles of Salt Lake City and emails you a daily report of new, changed, and removed job postings — pulling structured data straight from each company's applicant tracking system, no brittle web scraping.

Most tech companies don't hand-build their careers pages; they run them on an applicant tracking system (ATS) like Greenhouse, Lever, or SmartRecruiters, and those platforms expose clean JSON endpoints listing every open role. Prospector pans those streams once a day, compares today's haul against yesterday's, and tells you only what changed.

## What it does

- Fetches current open roles from every company in your list via their ATS's JSON API.
- Normalizes Greenhouse, Lever, and SmartRecruiters into one common shape.
- Filters to local (Utah) and remote roles, with a keyword list you control.
- Diffs against the previous run to surface **new**, **changed**, and **removed/filled** postings.
- Writes a fresh snapshot and a human-readable report — ready to email.

No headless browser, no HTML parsing for tier-1 sources, no third-party dependencies. Python standard library only.

## How it works

Fetching is tiered, cheapest and most reliable first:

1. **ATS JSON API** (Greenhouse, Lever, SmartRecruiters) — structured, fast, and stable. It doesn't break when a company reshuffles its page layout. This covers most of Silicon Slopes.
2. **Plain HTTP + HTML parse** — for a company with a simple static careers page and no API. (Not needed yet by the current list.)
3. **Headless browser (Playwright)** — last resort, only for pages that render entirely in JavaScript with no reachable data source.

The daily run is a short pipeline: read the company list → fetch each source → normalize → filter by location → compare to the saved snapshot → write the new snapshot and report.

## Repo layout

| File | Purpose |
|------|---------|
| `companies.json` | Your source-of-truth list: each company's name, city, ATS, and slug. Also holds a `needs_identification` backlog. |
| `jobmonitor.py` | The engine — fetch, normalize, filter, diff, report. |
| `snapshot.json` | Auto-generated. Yesterday's roles, used for the diff. Committed back on each run so history lives in git. |
| `report.md` | Auto-generated. The latest run's report — this is what gets emailed. |

## Quick start (local)

```bash
python3 jobmonitor.py
```

The first run establishes a baseline (no diff yet) and writes `snapshot.json`. Every run after that reports what changed since the last one.

## Configuration

**Companies** — edit `companies.json`:

```json
{ "name": "Lucid Software", "city": "South Jordan", "ats": "greenhouse", "slug": "lucidsoftware" }
```

`ats` is one of `greenhouse`, `lever`, or `smartrecruiters`. `slug` is the company's identifier on that platform.

**Location and remote filtering** — the toggles live at the top of `jobmonitor.py`:

- `LOCAL_KEYWORDS` — city/state terms that count as local. Add or trim to taste.
- `KEEP_REMOTE` — when `True`, roles marked remote are kept even if they aren't obviously Utah.
- `LOCAL_ONLY` — set `False` to track every role regardless of location.

## Adding a company

1. Find its careers page and identify the ATS. Quick test — a live endpoint returns JSON; a bad slug 404s (Greenhouse/Lever) or returns an empty-but-valid response (SmartRecruiters, so verify the count is non-zero):
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs`
   - Lever: `https://api.lever.co/v0/postings/<slug>?mode=json`
   - SmartRecruiters: `https://api.smartrecruiters.com/v1/companies/<slug>/postings`
2. Add an entry to `companies.json`.
3. Run once to fold it into the baseline.

Companies on Workday, iCIMS, or a custom stack (several of the big Silicon Slopes names) need a per-company endpoint — that's the tier-2 work tracked in `needs_identification`.

## Running it daily with GitHub Actions

This runs Prospector on a schedule, commits the updated snapshot back to the repo, and emails you the report — no server to maintain. Save as `.github/workflows/prospector.yml`:

```yaml
name: prospector
on:
  schedule:
    - cron: "0 13 * * *"   # 7:00 AM Mountain (MDT); adjust for your timezone
  workflow_dispatch:        # lets you trigger a run manually from the Actions tab

permissions:
  contents: write           # allows committing the updated snapshot

jobs:
  prospect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run prospector
        run: python jobmonitor.py

      - name: Commit updated snapshot
        run: |
          git config user.name  "prospector-bot"
          git config user.email "actions@users.noreply.github.com"
          git add snapshot.json report.md
          git commit -m "Daily run: $(date -u +%Y-%m-%d)" || echo "No changes"
          git push

      - name: Email the report
        uses: dawidd6/action-send-mail@v3
        with:
          server_address: smtp.gmail.com
          server_port: 465
          secure: true
          username: ${{ secrets.MAIL_USERNAME }}
          password: ${{ secrets.MAIL_PASSWORD }}
          from: Prospector Bot
          to: ${{ secrets.MAIL_TO }}
          subject: "Prospector — daily jobs report"
          body: file://report.md
```

### Credentials — you set these, not this repo

Do **not** put your email password in any file. In the repo's **Settings → Secrets and variables → Actions**, add three secrets yourself:

- `MAIL_USERNAME` — the sending Gmail address.
- `MAIL_PASSWORD` — a Gmail **App Password** (create one under your Google account's security settings; a normal password won't work with 2FA and shouldn't be used here anyway).
- `MAIL_TO` — where the report goes.

The workflow references these by name only, so your credentials never touch the code or the git history.

A note on the email body: `report.md` is Markdown, which most mail clients show as clean, readable plain text. If you'd rather it render with formatting, generate an HTML version in `jobmonitor.py` and swap `body:` for `html_body:`.

## Roadmap

- **Relevance filter** — a scoring pass that ranks each surviving role against a short description of what you want (frontend, microservices, seniority), so the report is signal, not volume. A cheap keyword pass first, an optional LLM pass on the survivors.
- **Tier-2 coverage** — pin down the ATS endpoints for the larger Workday-based employers in the radius (Adobe, Domo, Pluralsight, and others in `needs_identification`).
- **Source health alerts** — flag when a company's endpoint suddenly returns zero, which usually means they migrated ATS and the slug needs updating.

## Notes

Everything here reads public careers-page data through the same endpoints the companies' own job boards use. Be a good citizen: one run a day is plenty, and the ATS APIs are rate-friendly at that cadence.
