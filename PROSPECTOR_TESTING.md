# Prospector — testing and operating guide

Written for someone who does **not** write Python. Every command here is copy-paste. None of
them send email, and none of them can damage the live data — the safety rules are explained
below so you can trust that.

Run everything from the project folder:

```bash
cd ~/Documents/git-hub/prospector
```

---

## The two files you might ever edit

| File | What it does |
|---|---|
| `feedback_lisa.json` | Tells Prospector which roles to stop showing, and which you applied to |
| `settings.json` | Run-wide switches (age window, whether AI scoring is on, whether email is sent at all) |
| `profiles.json` | One line per person — including `email`, which pauses just that person's daily email |

Everything else is either generated or is code. You should not need to edit Python.

---

## 1. Running the tests

```bash
python3 test_jobmonitor.py
```

**What you want to see** — the last two lines:

```
Ran 161 tests in 0.11s
OK
```

`OK` means everything passed. If you instead see `FAILED (failures=2)`, the lines beginning
`FAIL:` name what broke. Copy the whole output to Claude and ask it to fix it — you do not
need to interpret it.

These tests never touch the internet, never call the AI, and never touch the real data files.
They finish in well under a second, so run them any time you're unsure.

---

## 2. Generating a safe sample report

This is the important one. It produces a **real report you can open in a browser**, without
sending email and without changing any live data.

### Free version (no AI cost, fake scores — good for checking layout)

```bash
python3 jobmonitor.py --dry-run --fake-fit
```

Then open the result:

```bash
open .dryrun/report_lisa.html
open .dryrun/report_chad.html
```

The report will have a **red warning banner** saying the scores are simulated. That banner is
deliberate — it means you're looking at made-up numbers and only the *layout* is meaningful.

### Real version (uses the AI, costs a small amount)

Only works if an API key is available in your shell. Same command, without `--fake-fit`:

```bash
python3 jobmonitor.py --dry-run
```

No banner means the scores are real.

### Why this is safe

- `--dry-run` writes everything into a throwaway `.dryrun/` folder.
- It **copies** the live snapshots in first, so the comparison is realistic — but it writes
  only to the copies.
- It refuses to signal GitHub Actions to send email, even if it's running inside Actions.
- You'll see this printed at the top, every time:

```
[safe run] snapshots → .../.dryrun
[safe run] reports   → .../.dryrun
[safe run] production snapshots and reports are untouched.
```

If you don't see that, you are **not** in a safe run — stop and add `--dry-run`.

### Other useful flags

| Command | What it does |
|---|---|
| `python3 jobmonitor.py --list` | Show the profiles and whether they're switched on |
| `python3 jobmonitor.py --dry-run --profile lisa` | Only Lisa, still safe |
| `python3 jobmonitor.py --no-fit` | Skip AI scoring entirely (no cost) |
| `python3 jobmonitor.py --help` | Full list of options |

---

## 3. Giving feedback on roles (the file that controls repetition)

Prospector shows good roles again on later days, because people rarely apply the first day
they see something. **Feedback is how you stop that** for a specific role.

### The easy way

After any run, open the generated template:

```bash
open feedback_template_lisa.json
```

It lists every role currently in play, already filled in with the exact identifiers. Find the
role you care about, copy that whole block, and paste it into the `"entries"` list in
`feedback_lisa.json` — then set the `"status"`.

### What to write

```json
{
  "entries": [
    { "key": "Podium::8080202", "status": "not_interested", "note": "too junior" },
    { "company": "Health Catalyst", "title": "Implementation Manager", "status": "applied", "date": "2026-07-30" }
  ]
}
```

You can identify a role **either** by its `key` (copied from the template) **or** by
`company` + `title` together, whichever is easier. `note` and `date` are for you; Prospector
ignores them.

### The statuses

| Status | What happens |
|---|---|
| `applied` | Never recommended again. Shows under **Hiring Progress** once the posting disappears. |
| `already_applied` | Hidden from recommendations. |
| `not_interested` | Hidden permanently. |
| `too_technical` | Hidden, and flagged in the weekly audit as a bad recommendation. |
| `wrong_function` | Hidden, and flagged as a bad recommendation. |
| `wrong_industry` | Hidden, and flagged as a bad recommendation. |
| `interested` | **Keeps showing** until it closes or ages out, with an ★ Interested badge. |

### Checking you didn't break the file

```bash
python3 -c "import json; d=json.load(open('feedback_lisa.json')); print('OK —', len(d['entries']), 'entries')"
```

If that prints `OK — 3 entries`, the file is valid. If it prints a red error, you most likely
missed a comma or a closing brace. A broken file is **not** dangerous — Prospector prints a
warning and carries on as if there were no feedback — but your feedback won't take effect
until it's fixed.

---

## 4. Checking GitHub Actions

Two scheduled jobs:

| Workflow | When | What it does |
|---|---|---|
| **prospector** | Daily, 13:00 UTC (~7 AM Mountain) | Fetches jobs, scores, emails whoever's report changed |
| **prospector-audit** | Mondays, 15:00 UTC | Writes `AUDIT_lisa.md` / `AUDIT_chad.md`. No emails, no AI, no job-site requests. |
| **prospector-logos** | Manual only | Refreshes company logos and commits them. Run it after adding companies. No emails, no AI. |

### To check a run

1. Go to the repo on GitHub → **Actions** tab.
2. Click the most recent **prospector** run.
3. Green check = it worked. Red X = something failed; click the failed step to see why.

### Refreshing company logos

New companies show a coloured monogram until their logo is fetched. To fix that:
Actions tab → **prospector-logos** → **Run workflow**. It fetches only what's missing,
verifies each file is a real image, commits them, and needs nothing from you. Tick
**force** only if you want every logo re-downloaded.

If a logo can't be fetched, the run still succeeds — that one company keeps its monogram and
is retried next time. Look for `removed` in the run summary to see which.

### To run it right now without waiting

Actions tab → **prospector** → **Run workflow**. Two useful boxes:

- **chad_only** — email only Chad, skip Lisa. Good for testing.
- **force_email** — send even if nothing changed.

### Useful things in the run log

Open the **Run prospector** step and look for:

- `Fetched N local roles` — how much came back. A sudden drop means a source problem.
- `Fetch window widened to 30d` — normal; Lisa uses 30 days, Chad 7.
- `held N role(s) from M failed source(s)` — a job site didn't respond, and those roles were
  correctly **not** reported as closed. Normal and self-healing.
- `Fit scoring: N call(s) … (X% of prompt served from cache)` — the AI cost line. A high cache
  percentage is good.
- `[warn] fit schema rejected` — the AI returned something unusable for that role. It will be
  retried next run; the role still appears, just unscored. **If you see many of these, tell
  Claude** — it means the scoring format needs attention.
- `[warn] fit parse failed … Unterminated string` — the AI's answer got cut off mid-sentence.
  This happened on the first live run (10 of 147) and was fixed by giving it more room. If it
  comes back, tell Claude — the limit needs raising again.

---

## 4b. Pausing (and resuming) one person's daily email

**Chad's email is currently PAUSED. Lisa's is on.**

To change it, open `profiles.json`, find the person, and set:

```json
"email": false     // paused — no daily email
"email": true      // on — daily email as usual
```

Check it took effect:

```bash
python3 jobmonitor.py --list
#   [on ] [EMAIL PAUSED] chad     Chad — Software / Frontend / Microservices
#   [on ] [email on ]    lisa     Lisa — Transformation / Operations / Experience Leadership
```

To silence **everyone** at once, set `"email": { "enabled": false }` in `settings.json`
instead. That is the master switch; the per-person setting sits underneath it.

### What pausing does and does not do

A paused person's search **keeps running every morning.** Prospector still checks every
company, still works out what is new, and still writes their report into the repo — you can
open `report_chad.html` any day you feel like looking. The only thing that stops is the
email landing in the inbox.

That is on purpose, and it is why you should **not** use `"enabled": false` to pause
someone. `enabled: false` stops the work entirely, which freezes their snapshot on the day
it stopped. Switch it back on three months later and Prospector compares today against a
three-month-old picture, so the first email is a giant dump of everything that happened in
between. With `email: false`, you flip it back to `true` and the next morning's email is an
ordinary one-day list.

### Does pausing save money?

**Not on its own.** The only thing in Prospector that costs money is the AI fit scoring, and
Chad's profile already had scoring turned off (`"fit_scoring": false`), so his daily run
costs nothing to begin with. Pausing his email saves inbox noise, not dollars.

The AI spend is entirely Lisa's scoring. The knobs for that, cheapest first, are in
`settings.json`:

| Knob | Effect |
|---|---|
| `discovery.max_new_scored_per_run` | Hard ceiling on how many NEW roles get scored per day. Roles over the cap are kept and scored the next day — nothing is lost. Lower it to spend less per day. |
| `fit_scoring_enabled: false` | Stops **all** AI scoring. Reports still arrive, just unranked. |

Changing `FIT_MODEL` in `jobmonitor.py` to a cheaper model is the other lever, but that one
is code, so ask before touching it.

---

## 5. Reading the weekly audit

```bash
open AUDIT_lisa.md
```

Four sections worth your attention:

1. **Current picture** — how many roles are tracked and how they're rated.
2. **Possible false negatives** — roles the AI rejected that scored highest anyway. *If any
   look right to you, the scoring is too strict — say so and it can be loosened.*
3. **Fetched but filtered out** — roles that never reached you, and the exact rule that
   dropped each one. *If you see something here you'd have wanted, that rule is too tight.*
4. **Source health** — companies returning nothing. A company with a high
   `consecutive_zero_runs` count and a ⚠️ has probably changed job systems and needs its
   configuration updated.

Nothing in the audit changes anything by itself. It's a report for you to act on.

---

## 6. Recognizing common failures

| What you see | What it means | What to do |
|---|---|---|
| No email at all | Nothing changed since yesterday, so no email was sent — this is normal | Check the Actions run is green. To force one, use **force_email** |
| Email has no scores, just roles | AI scoring was off or failing | Check `ANTHROPIC_API_KEY` is still set in repo Settings → Secrets; look for `[warn]` lines in the log |
| `Source warnings` in the email | A job site didn't respond | Usually transient. Those roles are held, not reported closed. If the same company appears for days, check the audit's source-health section |
| A big "no longer match the current search rules" count | The search rules changed | Expected right after a rule change; it settles after one run. It does **not** mean roles closed |
| Lots of roles you don't want | The filter is too loose | Mark a few with `wrong_function` / `too_technical`, then check the audit's false-positive patterns |
| Too few roles | The filter is too tight | Check the audit's "Fetched but filtered out" section for the rule to blame |
| `FAILED` when running tests | A code problem | Copy the full output to Claude |
| Red banner on a report | You used `--fake-fit`; the scores are invented | Re-run without `--fake-fit` for real scores |

---

## 7. Rolling back

Everything before the V2 changes is saved at the tag `v1-pre-v2`.

### Undo all the V2 changes

```bash
cd ~/Documents/git-hub/prospector
git checkout v1-pre-v2 -- .
git commit -m "Roll back Prospector V2"
git push
```

The next scheduled run uses the old behavior.

### One important caveat about snapshots

`snapshot_*.json` files are **state** — they remember which roles have been seen and hold the
cached AI verdicts. Rolling back the *code* while keeping the *newer* snapshots is fine and is
what the command above does not change. But be aware:

- The old code cannot read the new verdict format, so it re-scores everything once (a small
  one-time cost). Nothing breaks.
- **Do not delete snapshots to "start clean."** That forces a full re-score *and* makes every
  currently-open role look brand new, producing one enormous email.

### Roll back just one file

```bash
git checkout v1-pre-v2 -- profiles.json     # e.g. only Lisa's search rules
git commit -m "Revert profiles.json"
git push
```

### See what's changed since V1

```bash
git diff v1-pre-v2 --stat
```

---

## Quick reference

```bash
python3 test_jobmonitor.py                      # run the tests
python3 jobmonitor.py --dry-run --fake-fit      # safe sample report, no cost
open .dryrun/report_lisa.html                   # look at it
python3 jobmonitor.py --list                    # list profiles + who is getting email
python3 audit.py                                # regenerate the audit now
git diff v1-pre-v2 --stat                       # what changed since V1
```
