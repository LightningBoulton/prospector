#!/usr/bin/env python3
"""
Offline test suite for jobmonitor.py.

Run:  python3 test_jobmonitor.py            <-- use this one (quiet)
  or: python3 -m unittest -v test_jobmonitor.py -W ignore::ResourceWarning

(jobmonitor.py opens files without a `with` block throughout — its established style — so
the bare `-m unittest` form prints harmless ResourceWarnings. Both commands test the same
thing; the first just suppresses that noise.)

Standard library only (`unittest`) — no pytest, no new dependencies.

TWO HARD RULES, enforced by the tests themselves:
  1. NO NETWORK. Every test builds postings by hand or injects a fake fetcher into
     `FETCHERS`. Nothing here calls an ATS or the Anthropic API. `enrich_with_fit` is
     always passed client=None, so scoring no-ops and costs nothing.
  2. NO PRODUCTION WRITES. Any test that exercises snapshots points
     `jobmonitor.SNAPSHOT_DIR` at a fresh temp directory (see `LaneTestCase.setUp`) and
     asserts at the end that the real snapshot files were not touched.
"""
import copy
import json
import os
import tempfile
import unittest

import jobmonitor as jm

HERE = os.path.dirname(os.path.abspath(__file__))


def posting(company, ext_id, title, location="Salt Lake City, UT", posted="2026-07-28",
            **extra):
    """A normalized posting, the way a fetcher would emit it (see jm._norm)."""
    p = {"key": f"{company}::{ext_id}", "company": company, "title": title,
         "location": location, "url": f"https://example.test/{ext_id}",
         "posted": posted, "salary": None, "description": "", "_ats": None,
         "_detail_url": None}
    p.update(extra)
    return p


def profile(name="tester", **extra):
    """A profile that keeps everything, so a test can isolate diff/snapshot behavior."""
    p = {"name": name, "label": f"{name} — test", "enabled": True,
         "match_groups": [], "exclude_any": []}
    p.update(extra)
    return p


def load_real_profile(name):
    """The actual committed profile, so regression tests bind to shipped config."""
    for p in json.load(open(os.path.join(HERE, "profiles.json")))["profiles"]:
        if p["name"] == name:
            return p
    raise AssertionError(f"no profile named {name!r} in profiles.json")


# --------------------------------------------------------------------------------------
# Location gates
# --------------------------------------------------------------------------------------

class TestLocationGates(unittest.TestCase):

    def setUp(self):
        self._intl = jm.ALLOW_INTL_REMOTE
        jm.ALLOW_INTL_REMOTE = False

    def tearDown(self):
        jm.ALLOW_INTL_REMOTE = self._intl

    def test_is_local_keeps_utah_cities(self):
        for loc in ["Lehi, UT", "South Jordan, Utah", "Draper", "Salt Lake City, UT",
                    "American Fork, UT", "Provo, Utah"]:
            self.assertTrue(jm.is_local(loc), f"should be local: {loc}")

    def test_is_local_keeps_us_remote(self):
        for loc in ["Remote", "Remote - US", "United States (Remote)"]:
            self.assertTrue(jm.is_local(loc), f"should be kept: {loc}")

    def test_is_local_drops_other_metros(self):
        for loc in ["Austin, TX", "New York, NY", "Seattle, WA", "Denver, CO"]:
            self.assertFalse(jm.is_local(loc), f"should be dropped: {loc}")

    def test_is_local_drops_international_remote(self):
        for loc in ["United Kingdom - Remote", "Remote (Germany)", "India - Remote"]:
            self.assertFalse(jm.is_local(loc), f"should be dropped: {loc}")

    def test_is_local_keeps_international_remote_that_also_names_utah(self):
        # A multi-region posting that includes a local city stays in.
        self.assertTrue(jm.is_local("Remote - United Kingdom or Lehi, UT"))

    def test_word_boundary_ut_does_not_match_inside_words(self):
        # The documented regression: substring matching read "So[ut]hampton" as Utah.
        self.assertFalse(jm.is_local("Southampton, England"))
        self.assertFalse(jm.is_local("Stuttgart"))

    def test_local_gate_does_not_confuse_south_jordan_with_jordan(self):
        # "Jordan" is deliberately omitted from INTERNATIONAL_MARKERS for this reason.
        self.assertTrue(jm.is_local("South Jordan, UT"))

    def test_is_us_remote_requires_a_remote_marker(self):
        self.assertFalse(jm.is_us_remote("Lehi, UT"))       # location-locked
        self.assertFalse(jm.is_us_remote("Austin, TX"))
        self.assertTrue(jm.is_us_remote("Remote - US"))
        self.assertTrue(jm.is_us_remote("Anywhere"))

    def test_is_us_remote_drops_non_us_remote(self):
        self.assertFalse(jm.is_us_remote("Remote - United Kingdom"))
        self.assertFalse(jm.is_us_remote("India (Remote)"))

    def test_is_us_remote_treats_bare_remote_as_us_eligible(self):
        self.assertTrue(jm.is_us_remote("Remote"))

    def test_allow_international_remote_switch(self):
        jm.ALLOW_INTL_REMOTE = True
        self.assertTrue(jm.is_local("United Kingdom - Remote"))
        self.assertTrue(jm.is_us_remote("Remote - United Kingdom"))


# --------------------------------------------------------------------------------------
# Profile title matching
# --------------------------------------------------------------------------------------

class TestMatchesProfile(unittest.TestCase):

    def test_word_boundary_short_tokens(self):
        # "coo" must not match "Coordinator"; this is load-bearing for Lisa.
        prof = profile(match_groups=[["coo"]])
        self.assertTrue(jm.matches_profile(posting("A", "1", "COO"), prof))
        self.assertFalse(jm.matches_profile(posting("A", "2", "Project Coordinator"), prof))

    def test_and_across_groups_or_within(self):
        prof = profile(match_groups=[["director", "vp"], ["operations", "strategy"]])
        self.assertTrue(jm.matches_profile(
            posting("A", "1", "Director, Business Operations"), prof))
        self.assertTrue(jm.matches_profile(
            posting("A", "2", "VP Strategy"), prof))
        # satisfies group 1 only
        self.assertFalse(jm.matches_profile(
            posting("A", "3", "Director, Engineering"), prof))
        # satisfies group 2 only
        self.assertFalse(jm.matches_profile(
            posting("A", "4", "Operations Analyst"), prof))

    def test_exclude_any_wins_over_match(self):
        prof = profile(match_groups=[["director"]], exclude_any=["engineering"])
        self.assertFalse(jm.matches_profile(
            posting("A", "1", "Director of Engineering"), prof))

    def test_empty_match_groups_keeps_everything(self):
        self.assertTrue(jm.matches_profile(posting("A", "1", "Anything At All"), profile()))


class TestChadProfileRegression(unittest.TestCase):
    """Chad's profile must behave exactly as before the V2 work. These assertions bind to
    the committed profiles.json, so an accidental edit to his config fails here."""

    def setUp(self):
        self.chad = load_real_profile("chad")

    def test_chad_config_unchanged_in_shape(self):
        self.assertTrue(self.chad.get("enabled"))
        self.assertEqual(self.chad.get("fit_mode"), "rank")
        self.assertEqual(self.chad.get("background_file"), "chad_background.json")
        self.assertTrue(self.chad.get("remote_search"))
        # Chad has no staffing lane and must not gain one silently.
        self.assertFalse(self.chad.get("staffing_search", False))
        self.assertEqual(len(self.chad["match_groups"]), 1,
                         "Chad is a single-group profile")

    def test_chad_keeps_engineering_titles(self):
        for title in ["Senior Software Engineer", "Frontend Developer",
                      "Staff Backend Engineer", "Platform Engineer",
                      "Full Stack Developer", "DevOps Engineer",
                      "Web Developer", "UI Engineer"]:
            self.assertTrue(jm.matches_profile(posting("A", "1", title), self.chad),
                            f"Chad should keep: {title}")

    def test_chad_excludes_non_engineering(self):
        for title in ["Account Executive", "Technical Recruiter",
                      "Software Engineering Intern", "QA Engineer",
                      "Sales Engineer", "Customer Support Specialist"]:
            self.assertFalse(jm.matches_profile(posting("A", "1", title), self.chad),
                             f"Chad should exclude: {title}")

    def test_chad_does_not_match_leadership_ops_titles(self):
        for title in ["Director, Business Transformation", "Chief of Staff",
                      "VP Customer Experience"]:
            self.assertFalse(jm.matches_profile(posting("A", "1", title), self.chad),
                             f"Chad should not match Lisa-shaped title: {title}")


# --------------------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------------------

class TestDiff(unittest.TestCase):

    def test_new_removed_changed(self):
        prev = [posting("A", "1", "Kept"), posting("A", "2", "Gone"),
                posting("A", "3", "Old Title")]
        curr = [posting("A", "1", "Kept"), posting("A", "3", "New Title"),
                posting("A", "4", "Fresh")]
        new, removed, changed = jm.diff(prev, curr)
        self.assertEqual([p["key"] for p in new], ["A::4"])
        self.assertEqual([p["key"] for p in removed], ["A::2"])
        self.assertEqual(len(changed), 1)
        old, cur = changed[0]
        self.assertEqual((old["title"], cur["title"]), ("Old Title", "New Title"))

    def test_diff_ignores_fields_other_than_key_and_title(self):
        prev = [posting("A", "1", "Same", location="Lehi, UT", salary=None)]
        curr = [posting("A", "1", "Same", location="Remote", salary="$1–$2")]
        new, removed, changed = jm.diff(prev, curr)
        self.assertEqual((new, removed, changed), ([], [], []))


# --------------------------------------------------------------------------------------
# Lane behavior: snapshots, source errors, isolation
# --------------------------------------------------------------------------------------

class LaneTestCase(unittest.TestCase):
    """Base class that redirects snapshot writes into a temp dir and proves, on teardown,
    that no production snapshot was modified."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_snapshot_dir = jm.SNAPSHOT_DIR
        jm.SNAPSHOT_DIR = self._tmp.name
        jm._SALARY_CACHE.clear()
        # Fingerprint the real snapshots so we can prove we never wrote to them.
        self._prod_before = {
            os.path.basename(f): os.path.getmtime(f)
            for f in sorted(__import__("glob").glob(os.path.join(HERE, "snapshot_*.json")))}

    def tearDown(self):
        jm.SNAPSHOT_DIR = self._orig_snapshot_dir
        after = {os.path.basename(f): os.path.getmtime(f)
                 for f in sorted(__import__("glob").glob(
                     os.path.join(HERE, "snapshot_*.json")))}
        self.assertEqual(self._prod_before, after,
                         "a test modified a PRODUCTION snapshot file")
        self._tmp.cleanup()

    def write_snapshot(self, name, roles, suffix=""):
        path = os.path.join(jm.SNAPSHOT_DIR, f"snapshot_{name}{suffix}.json")
        slim = [{k: v for k, v in r.items()
                 if k != "description" and not k.startswith("_")} for r in roles]
        json.dump(slim, open(path, "w"), indent=1)
        return path

    def read_snapshot(self, name, suffix=""):
        path = os.path.join(jm.SNAPSHOT_DIR, f"snapshot_{name}{suffix}.json")
        return json.load(open(path))


class TestSourceErrorHandling(LaneTestCase):
    """The bug this fixes: a company whose fetch raises contributes zero postings, so every
    role we knew about there used to read as 'removed' (i.e. reported as filled)."""

    def _prev(self):
        return [posting("Acme", "1", "Director, Operations"),
                posting("Acme", "2", "Director, Strategy"),
                posting("Beta", "9", "Director, Customer Experience")]

    def test_failed_source_does_not_mark_roles_removed(self):
        prof = profile("srcerr")
        self.write_snapshot("srcerr", self._prev())
        # Acme raised this run, so only Beta is in the pool.
        src = {"pool": [posting("Beta", "9", "Director, Customer Experience")],
               "errors": ["Acme (greenhouse/acme): HTTPError 500"],
               "failed": {"Acme"}}
        lane, changed = jm._run_lane(prof, src, None, "", "Test Lane")

        self.assertEqual(lane["removed"], [],
                         "roles from a failed source must NOT be reported as removed")
        self.assertEqual(sorted(p["key"] for p in lane["held"]), ["Acme::1", "Acme::2"])
        self.assertFalse(changed, "a source error alone is not a real change")

    def test_failed_source_roles_are_carried_forward_in_the_snapshot(self):
        # If held roles were dropped from state, they would return as brand-new tomorrow.
        prof = profile("srcerr2")
        self.write_snapshot("srcerr2", self._prev())
        src = {"pool": [posting("Beta", "9", "Director, Customer Experience")],
               "errors": ["Acme (greenhouse/acme): HTTPError 500"], "failed": {"Acme"}}
        jm._run_lane(prof, src, None, "", "Test Lane")

        keys = sorted(r["key"] for r in self.read_snapshot("srcerr2"))
        self.assertEqual(keys, ["Acme::1", "Acme::2", "Beta::9"],
                         "held roles must stay in the snapshot")

    def test_recovered_source_produces_no_spurious_new_roles(self):
        # Day 2: Acme's feed works again. Its roles must be unchanged, not "new".
        prof = profile("srcerr3")
        self.write_snapshot("srcerr3", self._prev())
        broken = {"pool": [posting("Beta", "9", "Director, Customer Experience")],
                  "errors": ["Acme: boom"], "failed": {"Acme"}}
        jm._run_lane(prof, broken, None, "", "Test Lane")

        healthy = {"pool": self._prev(), "errors": [], "failed": set()}
        lane, changed = jm._run_lane(prof, healthy, None, "", "Test Lane")
        self.assertEqual(lane["new"], [], "recovered roles must not resurface as new")
        self.assertEqual(lane["removed"], [])
        self.assertFalse(changed)

    def test_genuinely_absent_roles_are_still_reported_removed(self):
        # The control case: no source error, role really is gone.
        prof = profile("srcerr4")
        self.write_snapshot("srcerr4", self._prev())
        src = {"pool": [posting("Acme", "1", "Director, Operations"),
                        posting("Beta", "9", "Director, Customer Experience")],
               "errors": [], "failed": set()}
        lane, changed = jm._run_lane(prof, src, None, "", "Test Lane")

        self.assertEqual([p["key"] for p in lane["removed"]], ["Acme::2"])
        self.assertEqual(lane["held"], [])
        self.assertTrue(changed, "a real removal is a change and should still email")

    def test_all_sources_failing_reports_nothing_removed(self):
        prof = profile("srcerr5")
        self.write_snapshot("srcerr5", self._prev())
        src = {"pool": [], "errors": ["Acme: boom", "Beta: boom"],
               "failed": {"Acme", "Beta"}}
        lane, changed = jm._run_lane(prof, src, None, "", "Test Lane")

        self.assertEqual(lane["removed"], [])
        self.assertEqual(len(lane["held"]), 3)
        self.assertFalse(changed)
        self.assertEqual(len(self.read_snapshot("srcerr5")), 3,
                         "a total outage must not wipe state")


class TestFirstRunAndSnapshotWrite(LaneTestCase):

    def test_first_run_establishes_baseline_without_reporting_changes(self):
        prof = profile("fresh")
        src = {"pool": [posting("A", "1", "Director, Operations")],
               "errors": [], "failed": set()}
        lane, changed = jm._run_lane(prof, src, None, "", "Test Lane")
        self.assertTrue(lane["first_run"])
        self.assertEqual((lane["new"], lane["removed"], lane["changed"]), ([], [], []))
        self.assertTrue(changed, "first run counts as changed so the first email sends")

    def test_snapshot_strips_description_and_private_fields(self):
        prof = profile("slim")
        src = {"pool": [posting("A", "1", "Director, Operations",
                                description="a long description",
                                _ats="greenhouse", _detail_url="https://x.test/1")],
               "errors": [], "failed": set()}
        jm._run_lane(prof, src, None, "", "Test Lane")
        rec = self.read_snapshot("slim")[0]
        self.assertNotIn("description", rec)
        self.assertNotIn("_ats", rec)
        self.assertNotIn("_detail_url", rec)
        self.assertEqual(rec["key"], "A::1")


# --------------------------------------------------------------------------------------
# collect_pool: gates + failed-company tracking, with injected fetchers (no network)
# --------------------------------------------------------------------------------------

class TestCollectPool(unittest.TestCase):

    def setUp(self):
        self._orig_fetchers = dict(jm.FETCHERS)
        self._tmp = tempfile.TemporaryDirectory()
        self._intl = jm.ALLOW_INTL_REMOTE
        jm.ALLOW_INTL_REMOTE = False

    def tearDown(self):
        jm.FETCHERS.clear()
        jm.FETCHERS.update(self._orig_fetchers)
        jm.ALLOW_INTL_REMOTE = self._intl
        self._tmp.cleanup()

    def _config(self, companies):
        path = os.path.join(self._tmp.name, "cfg.json")
        json.dump({"companies": companies}, open(path, "w"))
        return path

    def test_failed_fetcher_is_recorded_and_does_not_abort_the_run(self):
        def ok(c):
            return [posting("Good", "1", "Director, Operations", location="Lehi, UT")]

        def boom(c):
            raise RuntimeError("endpoint down")

        jm.FETCHERS["fake_ok"] = ok
        jm.FETCHERS["fake_boom"] = boom
        cfg = self._config([
            {"name": "Bad", "ats": "fake_boom", "slug": "bad"},
            {"name": "Good", "ats": "fake_ok", "slug": "good"},
        ])
        pool, errors, failed = jm.collect_pool(config_path=cfg, gate=jm.is_local)

        self.assertEqual([p["key"] for p in pool], ["Good::1"],
                         "a healthy source still returns its roles")
        self.assertEqual(failed, {"Bad"})
        self.assertEqual(len(errors), 1)
        self.assertIn("Bad", errors[0])

    def test_gate_and_age_filters_apply(self):
        old = (jm.datetime.date.today() - jm.datetime.timedelta(days=40)).isoformat()
        recent = jm.datetime.date.today().isoformat()

        def mixed(c):
            return [posting("M", "keep", "Director, Ops", location="Lehi, UT", posted=recent),
                    posting("M", "toofar", "Director, Ops", location="Austin, TX",
                            posted=recent),
                    posting("M", "tooold", "Director, Ops", location="Lehi, UT", posted=old)]

        jm.FETCHERS["fake_mixed"] = mixed
        cfg = self._config([{"name": "M", "ats": "fake_mixed", "slug": "m"}])
        pool, errors, failed = jm.collect_pool(max_age_days=14, config_path=cfg,
                                              gate=jm.is_local)
        self.assertEqual([p["key"] for p in pool], ["M::keep"])
        self.assertEqual(failed, set())

    def test_missing_config_file_is_not_fatal(self):
        pool, errors, failed = jm.collect_pool(
            config_path=os.path.join(self._tmp.name, "nope.json"))
        self.assertEqual((pool, errors, failed), ([], [], set()))

    def test_unknown_date_is_kept(self):
        def undated(c):
            return [posting("U", "1", "Director, Ops", location="Lehi, UT", posted="")]

        jm.FETCHERS["fake_undated"] = undated
        cfg = self._config([{"name": "U", "ats": "fake_undated", "slug": "u"}])
        pool, _, _ = jm.collect_pool(max_age_days=7, config_path=cfg, gate=jm.is_local)
        self.assertEqual(len(pool), 1, "a posting we cannot date is never dropped")


# --------------------------------------------------------------------------------------
# Fit scoring: failure must be safe, cache must be predictable
# --------------------------------------------------------------------------------------

class FakeClient:
    """Minimal stand-in for anthropic.Anthropic — never touches the network."""

    def __init__(self, reply=None, raises=None):
        self._reply, self._raises = reply, raises
        self.calls = 0
        outer = self

        class _Block:
            def __init__(self, text):
                self.type, self.text = "text", text

        class _Msg:
            def __init__(self, text):
                self.content = [_Block(text)]

        class _Messages:
            def create(self, **kw):
                outer.calls += 1
                if outer._raises:
                    raise outer._raises
                return _Msg(outer._reply)

        self.messages = _Messages()


class TestScoreFitSafety(unittest.TestCase):

    CANDIDATE = {"name": "Test Person", "summary": "ops leader"}

    def _posting(self):
        return posting("A", "1", "Director, Business Operations")

    def test_valid_reply_parses(self):
        c = FakeClient(reply='{"fit": "yes", "score": 88, "reason": "strong ops mandate"}')
        r = jm.score_fit(self.CANDIDATE, self._posting(), c)
        self.assertEqual(r["fit"], "yes")
        self.assertEqual(r["score"], 88)
        self.assertEqual(r["reason"], "strong ops mandate")

    def test_reply_wrapped_in_prose_or_fences_still_parses(self):
        c = FakeClient(reply='Sure!\n```json\n{"fit":"maybe","score":50,"reason":"ok"}\n```')
        r = jm.score_fit(self.CANDIDATE, self._posting(), c)
        self.assertEqual(r["fit"], "maybe")
        self.assertEqual(r["score"], 50)

    def test_invalid_fit_value_falls_back_to_maybe(self):
        c = FakeClient(reply='{"fit": "definitely", "score": 70, "reason": "x"}')
        self.assertEqual(jm.score_fit(self.CANDIDATE, self._posting(), c)["fit"], "maybe")

    def test_malformed_json_returns_neutral_and_does_not_raise(self):
        c = FakeClient(reply="not json at all")
        r = jm.score_fit(self.CANDIDATE, self._posting(), c)
        self.assertEqual(r["fit"], "maybe")
        self.assertEqual(r["score"], -1, "a parse failure must be marked uncacheable")

    def test_api_exception_returns_neutral_and_does_not_raise(self):
        c = FakeClient(raises=RuntimeError("api down"))
        r = jm.score_fit(self.CANDIDATE, self._posting(), c)
        self.assertEqual(r["score"], -1)
        self.assertIn("scoring unavailable", r["reason"])

    def test_non_integer_score_returns_neutral(self):
        c = FakeClient(reply='{"fit": "yes", "score": "very high", "reason": "x"}')
        self.assertEqual(jm.score_fit(self.CANDIDATE, self._posting(), c)["score"], -1)


class TestFitCache(unittest.TestCase):

    CANDIDATE = {"name": "Test Person", "summary": "ops leader"}

    def test_cached_verdict_is_reused_and_costs_no_call(self):
        prof = profile("cache", background_file=None)
        fp = jm._bg_fingerprint(self.CANDIDATE)
        prev = [{"key": "A::1", "company": "A", "title": "T",
                 "fit_result": {"fit": "yes", "score": 90, "reason": "r", "bg": fp}}]
        matched = [posting("A", "1", "T")]
        c = FakeClient(reply='{"fit":"no","score":10,"reason":"fresh"}')

        # Patch load_background so we don't need a real file on disk.
        orig = jm.load_background
        jm.load_background = lambda p: self.CANDIDATE
        try:
            scored = jm.enrich_with_fit(matched, prev, prof, c)
        finally:
            jm.load_background = orig

        self.assertEqual(scored, 0, "an unchanged role must not be re-scored")
        self.assertEqual(c.calls, 0)
        self.assertEqual(matched[0]["fit_result"]["score"], 90)

    def test_changed_background_invalidates_cache(self):
        prof = profile("cache2", background_file=None)
        prev = [{"key": "A::1", "company": "A", "title": "T",
                 "fit_result": {"fit": "yes", "score": 90, "reason": "r",
                                "bg": "staleprint000"}}]
        matched = [posting("A", "1", "T")]
        c = FakeClient(reply='{"fit":"no","score":10,"reason":"rescored"}')
        orig = jm.load_background
        jm.load_background = lambda p: self.CANDIDATE
        try:
            scored = jm.enrich_with_fit(matched, prev, prof, c)
        finally:
            jm.load_background = orig

        self.assertEqual(scored, 1, "a background edit must force a re-score")
        self.assertEqual(matched[0]["fit_result"]["reason"], "rescored")
        self.assertEqual(matched[0]["fit_result"]["bg"],
                         jm._bg_fingerprint(self.CANDIDATE))

    def test_failed_verdict_is_not_cached(self):
        prof = profile("cache3", background_file=None)
        fp = jm._bg_fingerprint(self.CANDIDATE)
        prev = [{"key": "A::1", "company": "A", "title": "T",
                 "fit_result": {"fit": "maybe", "score": -1, "reason": "err", "bg": fp}}]
        matched = [posting("A", "1", "T")]
        c = FakeClient(reply='{"fit":"yes","score":77,"reason":"retried"}')
        orig = jm.load_background
        jm.load_background = lambda p: self.CANDIDATE
        try:
            scored = jm.enrich_with_fit(matched, prev, prof, c)
        finally:
            jm.load_background = orig

        self.assertEqual(scored, 1, "a failed verdict must be retried, not reused")
        self.assertEqual(matched[0]["fit_result"]["score"], 77)

    def test_no_client_is_a_no_op(self):
        prof = profile("cache4", background_file=None)
        matched = [posting("A", "1", "T")]
        orig = jm.load_background
        jm.load_background = lambda p: self.CANDIDATE
        try:
            self.assertEqual(jm.enrich_with_fit(matched, None, prof, None), 0)
        finally:
            jm.load_background = orig
        self.assertNotIn("fit_result", matched[0])


# --------------------------------------------------------------------------------------
# Age / freshness helpers
# --------------------------------------------------------------------------------------

class TestAgeHelpers(unittest.TestCase):

    def test_within_age(self):
        today = jm.datetime.date.today()
        recent = (today - jm.datetime.timedelta(days=3)).isoformat()
        old = (today - jm.datetime.timedelta(days=30)).isoformat()
        self.assertTrue(jm._within_age(recent, 14))
        self.assertFalse(jm._within_age(old, 14))
        self.assertTrue(jm._within_age(old, 0), "0 disables the age gate")
        self.assertTrue(jm._within_age(old, None))
        self.assertTrue(jm._within_age("", 14), "unknown date is always kept")
        self.assertTrue(jm._within_age("not-a-date", 14))

    def test_workday_relative_date_parsing(self):
        today = jm.datetime.date.today()
        self.assertEqual(jm._workday_date("Posted Today"), today.isoformat())
        self.assertEqual(jm._workday_date("Posted Yesterday"),
                         (today - jm.datetime.timedelta(days=1)).isoformat())
        self.assertEqual(jm._workday_date("Posted 5 Days Ago"),
                         (today - jm.datetime.timedelta(days=5)).isoformat())
        self.assertEqual(jm._workday_date("Posted 30+ Days Ago"),
                         (today - jm.datetime.timedelta(days=30)).isoformat())
        self.assertEqual(jm._workday_date("nonsense"), "")


if __name__ == "__main__":
    # warnings="ignore" hides ResourceWarnings from jobmonitor.py's bare open() calls.
    unittest.main(verbosity=2, warnings="ignore")
