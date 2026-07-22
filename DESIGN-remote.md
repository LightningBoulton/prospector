# DESIGN — World/Remote search lane

Status: **proposal for review** (no code yet). Decisions locked with Chad:
US-only remote · results serve the existing per-person profiles · design doc before build.

## 1. Goal

Add a second discovery lane that surfaces **US-based remote** roles from *any* company,
matched to each person's existing profile — **in addition to**, and without changing, the
current local (Silicon Slopes) company search.

The hard problem with remote job feeds is stale/spam/fake postings. We solve it by treating
the feed as **discovery only** and validating every candidate against the company's own ATS
board before we keep it — reusing the fetchers this app already has.

## 2. Core principle: feed discovers, ATS validates

```
FEED = "here's a company + a role that might exist"   (untrusted, secondhand)
ATS  = "here's the role, live, on the company's own board"  (first-party, trusted)
```

We never report feed data. We report **ATS data** for roles we could independently confirm.
A spammy/stale feed can't pollute results — anything we can't confirm on the source board is
dropped, and every field we show (title, URL, date, salary, description) comes from the ATS.

This is not new capability: `discover.py` already resolves company→ATS (`probe_ats`,
`_slug_variants`) and `jobmonitor.py` already fetches/normalizes every ATS. The remote lane
is mostly **wiring existing parts together** plus a feed front-end.

## 3. Pipeline

```
remote feed(s)
   │   candidate = {company, title, apply_url, location, source}
   ▼
(a) ATS-URL gate      keep only postings whose apply_url is a known ATS URL;
                      parse {ats, slug[, wd_host, site]} straight from the link
   ▼
(b) US-remote gate    keep US / US-Remote / Anywhere(US); drop explicit non-US
   ▼
(c) keyword pre-filter  match against the UNION of remote-enabled profiles' terms
                        (cheap, title-only — cut volume BEFORE any network calls)
   ▼
(d) VALIDATE          fetch that company's real board via existing fetch_<ats>();
                      confirm the role is present (by ATS id / title);
                      take the ATS's canonical posting. Drop if absent.
   ▼
(e) registry upsert   append validated companies to remote_companies.json
   ▼
(f) dedup + age gate  dedup by `key` vs local pool + remote_companies; drop too-old
   ▼
(g) per-profile       matches_profile + enrich_with_fit (existing) → remote report
```

Steps (d)–(g) reuse existing engine code unchanged. Only (a)–(c) + the registry are new.

## 4. Anti-junk strategy (three layers)

1. **Curated feeds** as input, not raw aggregators.
2. **Require an ATS apply URL.** Scammers/aggregators link to LinkedIn/Indeed/custom pages;
   real employers on Greenhouse/Lever/Ashby link to their board. This single rule removes
   most junk *and* makes company→ATS resolution trivial and reliable (no name-guessing).
3. **Live re-validation + `max_posting_age_days`.** A posting pulled from the ATS is current
   by definition; stale ones already fell off the board and fail validation.

## 5. Config & data model

### `remote_companies.json` (new — the self-building registry)
Same entry shape as `companies.json`, plus provenance. Auto-appended when a company first
validates; read directly on later runs so the feed only needs to find *new* companies.
```json
{
  "companies": [
    { "name": "Acme", "ats": "greenhouse", "slug": "acme", "domain": "acme.com",
      "first_seen": "2026-07-21", "source": "remotive", "last_validated": "2026-07-21" }
  ]
}
```

### `settings.json` (add a block)
```json
"remote_search": {
  "enabled": true,
  "us_only": true,
  "feeds": ["remotive"],
  "max_feed_items": 500,
  "max_validations_per_run": 100
}
```

### `profiles.json` (opt-in flag, reuse everything else)
```json
{ "name": "chad", "...": "...", "remote_search": true }
```
A profile with `remote_search: true` is also run against the remote pool, using its **same**
`match_groups` / `exclude_any` / `background_file` / `fit_mode`.

## 6. New code (small surface)

- `fetch_remote_feed(feeds)` → candidate list from the configured feed API(s). Start with
  **Remotive** (curated, free JSON, apply links commonly point at ATSes).
- `_ats_from_url(url)` → `{ats, slug[, wd_host, site]}` or `None`. Patterns:
  - `boards.greenhouse.io/<slug>` / `job-boards.greenhouse.io/<slug>` → greenhouse
  - `jobs.lever.co/<slug>` → lever
  - `jobs.ashbyhq.com/<slug>` → ashby
  - `<tenant>.<wdN>.myworkdayjobs.com/<site>` → workday (later phase)
  - `jobs.smartrecruiters.com/<slug>` → smartrecruiters (later phase)
- `validate_on_ats(candidate, cache)` → fetch the board once per company (cache by slug),
  find the role, return the canonical normalized posting or `None`.
- `collect_remote_pool()` → orchestrates §3 (a)–(f); returns postings in the **same
  normalized shape** as the local pool so `run_profile` needs no changes.

## 7. Geography (US-only remote)

A dedicated remote gate (separate from the local `is_local`): keep `Remote`, `Anywhere`,
`US`, `United States`, `Remote - US`; drop explicit non-US (`Remote - UK`, `EMEA`, etc.),
reusing the existing `INTERNATIONAL_MARKERS` list inverted. Bare `Remote`/`Anywhere` with no
country is treated as US-eligible (most US listings say only "Remote") — flagged as a known
soft edge.

## 8. Reporting / email

Each person gets **one email** with two sections: **Local** (existing) and **US-Remote**
(new). Backed by two independent snapshots per profile so diffs stay clean:
- `snapshot_<name>.json` (local, unchanged) + `snapshot_<name>_remote.json` (new)
- The `<name>_changed` email gate ORs the two lanes.
- Logos work identically (remote companies get prefetched via `fetch_logos.py` reading
  `remote_companies.json` too).

Alternative (if preferred): a separate remote report/email per person. Decision deferred.

## 9. Volume, cost, politeness

- Keyword pre-filter (§3c) runs **before** validation → few network calls.
- `max_validations_per_run` caps ATS board fetches; one fetch per company per run (cached).
- The registry means steady-state runs mostly re-fetch known boards; the feed only adds new
  ones — cost trends *down* over time, not up.
- Fit scoring only scores **new** validated matches (existing snapshot cache) → LLM cost
  ∝ new remote roles, not total.
- One run/day; polite to feed APIs and ATS endpoints (same as today).

## 10. Phasing

- **MVP:** Remotive feed · Greenhouse/Lever/Ashby URL resolution only · US-only · per-person
  · two-section email · registry auto-append · caps. Prove ATS-link coverage & survival rate.
- **v2:** add feeds (RemoteOK/WWR/Himalayas), Workday/SmartRecruiters resolution, `fit_mode:
  "filter"` for the remote lane, richer dedup.

## 11. Non-goals

- No LinkedIn/Indeed scraping (no clean API, hostile ToS).
- No keeping unvalidated postings, ever.
- No change to the local company search or its outputs.

## 12. Open risks (measure in a prototype before committing)

- **ATS-link coverage:** what % of feed items have a usable ATS apply URL? (Determines yield.)
- **Survival rate:** of those, how many still validate on the board? (Determines signal.)
- **Feed API stability/ToS:** confirm Remotive's terms allow this cadence.
- **Validation false-negatives:** role present but title reworded → matching heuristic
  (prefer ATS id from the URL when present; fall back to normalized-title match).
- **Volume/cost** if keyword filters are loose — start strict.

## 13. Recommended first step after this doc

A throwaway probe: pull Remotive once, run §3 (a)–(d) on a sample, and report
`feed items → ATS-URL → keyword-match → validated`. That single measurement tells us whether
the whole approach yields enough real roles to be worth building — before writing production code.
