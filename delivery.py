#!/usr/bin/env python3
"""
delivery.py — run timing, the DST-safe schedule guard, and the one-email-a-day ledger.

THE PROBLEM THIS SOLVES
-----------------------
Prospector feeds a morning review. Delivery had drifted from ~7:45-8:05 a.m. Mountain to
anywhere between 11 a.m. and 5 p.m., which makes the report useless on the day it is about.
Three separate causes, three separate fixes, all of them here:

  1. GitHub Actions cron is UTC-only and has no notion of DST, so a fixed "0 13 * * *"
     is 7:00 a.m. in summer and 6:00 a.m. in winter. `should_run_now` lets the workflow
     schedule BOTH candidate UTC times and run only on the one that is actually 6:45 in
     America/Denver today.
  2. A scheduled run that fails, or that GitHub delays past the review window, previously had
     no recovery — it just landed whenever it landed. `catchup_needed` lets a later,
     cheap-to-schedule run decide for itself whether the morning run already succeeded.
  3. Adding a catch-up run creates the risk this module exists to remove: TWO emails in one
     day. `already_delivered` is the ledger that makes the canonical daily email canonical —
     the send is gated on it, and only a CONFIRMED delivery writes to it.

`run_log.json` is committed state, like the snapshots and the lifecycle DB. It is what lets
one run answer questions about another run that happened on a different machine hours
earlier.

Standard library only, like everything else here.
"""

import datetime
import json
import os

LOG_NAME = "run_log.json"
TIMEZONE = "America/Denver"
KEEP_RUNS = 30                 # enough to see a week of drift without bloating the diff

# The daily target, from the requirement: start 6:45 a.m. Mountain, email in the reader's
# hands by 7:15-7:30. `TOLERANCE_MINUTES` is what makes the DST guard survive GitHub's
# scheduling jitter, which routinely runs a cron several minutes late.
TARGET_HOUR = 6
TARGET_MINUTE = 45
# 59, not 60, and that single minute is load bearing. The workflow schedules BOTH 12:45 and
# 13:45 UTC because only one of them is 06:45 in Denver on a given date — but the other one is
# exactly 07:45 local, so a 60-minute tolerance would let BOTH fire and produce two runs a day.
# At 59 the wrong-season cron is always rejected, while a right-season cron that GitHub starts
# up to 59 minutes late still produces the morning email. Anything later than that is the
# catch-up run's job, not this window's.
TOLERANCE_MINUTES = 59


def _zone():
    """America/Denver, or None if this machine has no tz database.

    Falling back to None rather than raising is deliberate: a missing tzdata must degrade the
    guard to 'run anyway', never block the morning run entirely. A report an hour early is a
    far smaller failure than no report."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:                                    # noqa: BLE001
        return None


def now_local():
    """Current time in America/Denver (UTC if the tz database is unavailable)."""
    zone = _zone()
    return datetime.datetime.now(zone) if zone else datetime.datetime.now(
        datetime.timezone.utc)


def stamp(dt=None):
    return (dt or now_local()).isoformat(timespec="seconds")


def today_local():
    return now_local().date().isoformat()


def minutes_from_target(dt=None, hour=TARGET_HOUR, minute=TARGET_MINUTE):
    """Signed minutes between `dt` (local) and today's target time. Negative = early."""
    dt = dt or now_local()
    return (dt.hour * 60 + dt.minute) - (hour * 60 + minute)


def should_run_now(dt=None, hour=TARGET_HOUR, minute=TARGET_MINUTE,
                   tolerance=TOLERANCE_MINUTES):
    """Is NOW the local time this daily run is meant to happen?

    The workflow schedules both UTC candidates (12:45 and 13:45) because only one of them is
    6:45 in Denver on any given date; this returns True for that one and False for the other.
    Using the real zone rather than a hardcoded offset is what makes the DST changeover a
    non-event — there is no date in March or November to remember.

    The window is generous on the late side (59 minutes) because GitHub routinely starts a
    cron behind schedule and a run that fires 40 minutes late should still produce the morning
    email — but never 60, or the off-season cron at exactly +1h would fire too. See the
    TOLERANCE_MINUTES note."""
    delta = minutes_from_target(dt, hour, minute)
    return -15 <= delta <= tolerance


def should_start(log, force=False, catchup=False, dt=None, today=None):
    """The workflow's one question: should this job do the day's work, or exit quietly?

    Returns (bool, reason). Three guards, in order of authority:

      1. A run that already COMPLETED today wins over everything except --force. This is what
         makes a catch-up schedule safe to add and what stops the two DST cron entries from
         both doing the work on a day when GitHub is running late.
      2. Inside the morning window (see `should_run_now`) — the ordinary path.
      3. A catch-up slot, which does the work only BECAUSE guard 1 did not fire: the morning
         run failed, or never started.

    The email is gated separately on the delivery ledger (`already_delivered`), so even a
    belt-and-braces failure here can only cost a duplicate RUN, never a duplicate email."""
    today = today or today_local()
    if force:
        return True, "forced (manual dispatch)"
    if completed_today(log, today):
        return False, "a run already completed successfully today"
    if should_run_now(dt):
        return True, "inside the morning delivery window"
    if catchup:
        return True, "catch-up: no successful run recorded today"
    local = (dt or now_local()).strftime("%H:%M")
    return False, (f"local time {local} is outside the "
                   f"{TARGET_HOUR:02d}:{TARGET_MINUTE:02d} (+{TOLERANCE_MINUTES}m) window")


# ---- the log ---------------------------------------------------------------------------------

def path(directory):
    return os.path.join(directory, LOG_NAME)


def load(directory):
    """Read the run log. A missing or corrupt file yields an empty one rather than an
    exception — the same rule the lifecycle DB follows, and for the same reason: losing the
    timing history is a nuisance, failing the morning run is not."""
    p = path(directory)
    if not os.path.exists(p):
        return {"runs": [], "delivery": {}}
    try:
        raw = json.load(open(p))
    except Exception as e:                               # noqa: BLE001
        print(f"[warn] {LOG_NAME} unreadable ({type(e).__name__}); starting a fresh log.")
        return {"runs": [], "delivery": {}}
    return {"runs": raw.get("runs") or [], "delivery": raw.get("delivery") or {}}


def save(log, directory):
    doc = {"_comment": (
        "Prospector run + delivery log. `runs` is the timing record the morning-delivery "
        "requirement asks for (scheduled, started, completed, emailed) — newest last. "
        "`delivery` is the ledger that keeps the daily email CANONICAL: a profile whose "
        "last_sent is today will not be emailed again, which is what makes a catch-up run "
        "safe to schedule. Only a CONFIRMED send writes to it (see --confirm-sent)."),
        "timezone": TIMEZONE,
        "runs": log.get("runs", [])[-KEEP_RUNS:],
        "delivery": log.get("delivery", {})}
    json.dump(doc, open(path(directory), "w"), indent=1)


def start_run(log, scheduled_for="", trigger="", dt=None):
    """Open a run record and return it. `scheduled_for` is what the workflow was ASKED to do
    (the cron expression or dispatch time); `started_at` is when the job actually began — the
    gap between them is the drift the requirement asks us to make visible."""
    dt = dt or now_local()
    run = {"date": dt.date().isoformat(),
           "scheduled_for": scheduled_for or "",
           "trigger": trigger or "",
           "started_at": stamp(dt),
           "start_delay_min": minutes_from_target(dt),
           "completed_at": "", "status": "running", "emailed": {}}
    log.setdefault("runs", []).append(run)
    return run


def finish_run(run, status="ok", dt=None, detail=""):
    dt = dt or now_local()
    run["completed_at"] = stamp(dt)
    run["status"] = status
    if detail:
        run["detail"] = detail
    try:
        started = datetime.datetime.fromisoformat(run["started_at"])
        run["duration_min"] = round((dt - started).total_seconds() / 60, 1)
    except Exception:                                    # noqa: BLE001
        pass
    return run


def completed_today(log, today=None):
    """Did a run finish successfully today? This is what a catch-up run asks before doing
    any work — it is the difference between a safety net and a second daily job."""
    today = today or today_local()
    return any(r.get("date") == today and r.get("status") == "ok"
               for r in log.get("runs", []))


def catchup_needed(log, today=None):
    return not completed_today(log, today)


# ---- the delivery ledger ---------------------------------------------------------------------

def already_delivered(log, profile_name, today=None):
    """Has this person's canonical email for today already gone out?

    The send gate reads this. It is keyed on a CONFIRMED delivery, not on a rendered report,
    for the same reason `ever_shown` is: rendering is not sending, and a run that rendered a
    report and then failed to mail it must be allowed to try again."""
    today = today or today_local()
    return (log.get("delivery", {}).get(profile_name, {}) or {}).get("last_sent") == today


def record_delivery(log, profile_names, dt=None):
    """Write the ledger entry that makes today's email the canonical one. Called only from
    --confirm-sent, which the workflow runs after the mail step reports success."""
    dt = dt or now_local()
    entries = log.setdefault("delivery", {})
    for name in profile_names:
        entry = entries.setdefault(name, {})
        entry["last_sent"] = dt.date().isoformat()
        entry["last_sent_at"] = stamp(dt)
        entry["sends"] = int(entry.get("sends") or 0) + 1
    for run in reversed(log.get("runs", [])):
        if run.get("date") == dt.date().isoformat():
            run.setdefault("emailed", {})
            for name in profile_names:
                run["emailed"][name] = stamp(dt)
            break
    return log


def timing_text(log, today=None):
    """The four timestamps the requirement asks to be logged, for today's runs."""
    today = today or today_local()
    rows = [r for r in log.get("runs", []) if r.get("date") == today]
    if not rows:
        return "No run recorded today."
    out = [f"--- delivery timing ({TIMEZONE}) ---"]
    for r in rows:
        emailed = ", ".join(f"{k} {v[11:19]}" for k, v in (r.get("emailed") or {}).items())
        out.append(
            f"  scheduled {r.get('scheduled_for') or '(manual)'} · "
            f"started {r.get('started_at', '')[11:19]} "
            f"({r.get('start_delay_min', 0):+d} min vs {TARGET_HOUR:02d}:{TARGET_MINUTE:02d}) · "
            f"completed {(r.get('completed_at') or '—')[11:19]} · "
            f"status {r.get('status')}"
            + (f" · emailed {emailed}" if emailed else " · emailed —"))
    return "\n".join(out)
