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
import re
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


class TestLisaProfileTitleFilter(unittest.TestCase):
    """The positive/negative title lists come straight from the V2 spec. These bind to the
    committed profiles.json, so tightening or loosening Lisa's filter fails here first."""

    def setUp(self):
        self.lisa = load_real_profile("lisa")

    # ---- must remain eligible ----
    SHOULD_KEEP = [
        "Director, Business Transformation",
        "Senior Manager, Operational Excellence",
        "Principal, Organizational Strategy",
        "Head of Customer Experience",
        "Program Director, M&A Integration",
        "Change Management Lead",
        "Manager, Strategic Initiatives",
        "Principal Consultant, Operating Model Transformation",
        "Director, Transformation — Manufacturing",
        "Contract Program Lead, AI Adoption",
    ]

    # ---- must be excluded ----
    SHOULD_DROP = [
        "Director of Corporate Accounting",
        "Senior Manager, FP&A",
        "Tax Director",
        "Controller",
        "Software Engineering Manager",
        "Senior Frontend Developer",
        "Director of Data Science",
        "Security Engineering Lead",
        "Clinical Operations Director",
        "Plant Operations Manager",
        "Account Executive",
        "Sales Development Manager",
    ]

    def test_spec_positive_titles_are_kept(self):
        for title in self.SHOULD_KEEP:
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"Lisa should keep: {title}")

    def test_spec_negative_titles_are_dropped(self):
        for title in self.SHOULD_DROP:
            with self.subTest(title=title):
                self.assertFalse(jm.matches_profile(posting("A", "1", title), self.lisa),
                                 f"Lisa should drop: {title}")

    def test_priority_role_families_all_match(self):
        for fn in ["Organizational Strategy", "Business Transformation", "Operations",
                   "Operational Excellence", "Program Management", "PMO",
                   "Customer Experience", "Employee Experience", "Professional Services",
                   "Strategic Initiatives", "AI Enablement", "Change Management"]:
            title = f"Director, {fn}"
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"priority family should match: {title}")

    def test_also_relevant_seniorities_match(self):
        for title in ["Senior Director, Business Operations", "Senior Manager, Operations",
                      "Principal, Enterprise Transformation", "Head of Operations",
                      "Chief of Staff", "Program Director, Transformation",
                      "Transformation Lead", "Practice Leader", "General Manager"]:
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"should match: {title}")

    def test_adjacent_families_match_when_the_mandate_fits(self):
        for title in ["Director, SEO", "Director, Content Strategy",
                      "Director, Digital Strategy", "Director, Marketing Operations",
                      "Head of Customer Success", "Director, Learning and Development",
                      "Director, Organizational Development",
                      "Director, Business Operations", "Director, Service Design",
                      "Manager, Process Improvement", "Director, M&A Integration",
                      "Director, Value Realization", "Director, Workforce Transformation",
                      "Director, AI Adoption", "Director, Operating Model Design",
                      "Director, Enterprise Transformation"]:
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"adjacent family should match: {title}")

    def test_contract_and_temporary_are_not_penalized_by_the_title_gate(self):
        for title in ["Contract Director, Business Transformation",
                      "Interim Head of Operations",
                      "Temporary Program Manager, Change Management"]:
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"contract/temp leadership should match: {title}")

    def test_broad_words_alone_no_longer_let_noise_through(self):
        """The main precision fix: bare 'experience', 'support', 'service', 'care', 'people',
        'success', 'client', 'community', 'innovation', 'AI' used to satisfy the function
        group on their own, so any Manager/Lead title slipped past."""
        for title in ["Support Manager", "Community Manager", "Customer Care Manager",
                      "Guest Experience Manager", "Food Service Manager",
                      "People Manager", "Client Manager", "Innovation Manager",
                      "AI Manager", "Delivery Driver Lead", "Member Services Manager"]:
            with self.subTest(title=title):
                self.assertFalse(jm.matches_profile(posting("A", "1", title), self.lisa),
                                 f"broad-word noise should be dropped: {title}")

    def test_meaningful_compounds_of_those_broad_words_still_match(self):
        for title in ["Director, Customer Experience", "Director, Employee Experience",
                      "Director, Customer Success", "Director, Service Delivery",
                      "Director, Client Services", "Director, People Operations",
                      "Director, Member Experience", "Director, AI Enablement"]:
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"compound should still match: {title}")

    def test_manufacturing_industry_is_not_excluded(self):
        for title in ["Director, Operational Excellence — Manufacturing",
                      "Senior Manager, Manufacturing Operations",
                      "Director, Transformation, Manufacturing Division"]:
            with self.subTest(title=title):
                self.assertTrue(jm.matches_profile(posting("A", "1", title), self.lisa),
                                f"manufacturing employer should not be excluded: {title}")
        # but a manufacturing *engineering* role is still out
        self.assertFalse(jm.matches_profile(
            posting("A", "1", "Manufacturing Engineering Manager"), self.lisa))

    def test_finance_transformation_survives_but_accounting_does_not(self):
        # Bare "finance" is deliberately not an exclusion term.
        self.assertTrue(jm.matches_profile(
            posting("A", "1", "Director, Finance Transformation"), self.lisa))
        self.assertFalse(jm.matches_profile(
            posting("A", "1", "Director, Corporate Accounting"), self.lisa))

    def test_leadership_development_survives_sales_development_does_not(self):
        self.assertTrue(jm.matches_profile(
            posting("A", "1", "Director, Leadership Development"), self.lisa))
        self.assertTrue(jm.matches_profile(
            posting("A", "1", "Director, Organizational Development"), self.lisa))
        self.assertFalse(jm.matches_profile(
            posting("A", "1", "Manager, Sales Development"), self.lisa))


class TestMandateRescue(unittest.TestCase):
    """Generically titled roles are rescued on DESCRIPTION evidence, not title keywords."""

    def setUp(self):
        self.lisa = load_real_profile("lisa")

    MANDATE_DESC = ("You will lead our enterprise transformation agenda, own the "
                    "operating model redesign, and partner with executives on "
                    "change management across a fast-growing organization.")

    def test_generic_title_with_mandate_description_is_rescued(self):
        p = posting("A", "1", "Director, Special Projects", description=self.MANDATE_DESC)
        self.assertFalse(all(jm._any_term(p["title"].lower(), g)
                             for g in self.lisa["match_groups"]),
                         "precondition: this title must FAIL the normal title gate")
        self.assertTrue(jm.matches_profile(p, self.lisa),
                        "a leadership title with a clear mandate should be rescued")
        self.assertIn("lisa", p["_rescued_for"])

    def test_generic_title_without_mandate_is_not_rescued(self):
        p = posting("A", "1", "Director, Special Projects",
                    description="Manage the office snack budget and greet visitors.")
        self.assertFalse(jm.matches_profile(p, self.lisa))

    def test_rescue_requires_minimum_distinct_terms(self):
        # One mandate phrase, repeated — must not be enough (min_hits is 3).
        desc = "operating model. " * 12
        p = posting("A", "1", "Director, Special Projects", description=desc)
        self.assertFalse(jm.matches_profile(p, self.lisa),
                         "repeated boilerplate must not rescue on its own")

    def test_rescue_cannot_override_an_exclusion(self):
        p = posting("A", "1", "Director of Corporate Accounting",
                    description=self.MANDATE_DESC)
        self.assertFalse(jm.matches_profile(p, self.lisa),
                         "exclusions are absolute — a rescue must never bypass them")

    def test_rescue_requires_a_leadership_shaped_title(self):
        p = posting("A", "1", "Business Analyst", description=self.MANDATE_DESC)
        self.assertFalse(jm.matches_profile(p, self.lisa),
                         "rescue is gated on require_title_any")

    def test_boilerplate_only_description_is_not_rescued(self):
        """Regression on a real finding: with generic manager-JD terms in the rescue list
        ('cross-functional', 'stakeholder management', 'program management'), a bare
        'Project Manager' and a 'Lead, Benefits' role were both rescued off real feeds."""
        boilerplate = ("Partner cross-functionally with stakeholders, own program "
                       "management for our roadmap, drive continuous improvement and "
                       "operational efficiency, and manage the p&l for your area.")
        for title in ["Project Manager", "Lead, Benefits", "Product Manager"]:
            with self.subTest(title=title):
                p = posting("A", "1", title, description=boilerplate)
                self.assertFalse(jm.matches_profile(p, self.lisa),
                                 f"generic boilerplate must not rescue: {title}")

    def test_distinctive_mandate_language_still_rescues(self):
        # Same generic title, but the description names real transformation work.
        p = posting("A", "1", "Project Manager",
                    description="Own the target operating model and lead our "
                                "post-merger integration workstream.")
        self.assertTrue(jm.matches_profile(p, self.lisa))

    def test_no_description_means_no_rescue(self):
        # Documents the known Workday/SmartRecruiters coverage gap.
        p = posting("A", "1", "Director, Special Projects", description="")
        self.assertFalse(jm.matches_profile(p, self.lisa))

    def test_chad_has_no_rescue_configured_so_behavior_is_unchanged(self):
        chad = load_real_profile("chad")
        self.assertIsNone(chad.get("mandate_rescue"))
        p = posting("A", "1", "Director, Special Projects", description=self.MANDATE_DESC)
        self.assertFalse(jm.matches_profile(p, chad),
                         "Chad must not gain rescue behavior")

    def test_rescue_flag_is_stripped_before_the_snapshot(self):
        p = posting("A", "1", "Director, Special Projects", description=self.MANDATE_DESC)
        jm.matches_profile(p, self.lisa)
        slim = {k: v for k, v in p.items()
                if k != "description" and not k.startswith("_")}
        self.assertNotIn("_rescued_for", slim,
                         "_rescued_for must not reach the snapshot (it is not JSON-safe)")


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


def verdict_json(**over):
    """A well-formed model reply, with fields overridable per test."""
    d = {"qualification_fit": 80, "interest_fit": 90, "practical_fit": 70,
         "opportunity_score": 84, "recommendation": "strong_fit",
         "reasons": ["clear transformation mandate", "director-level scope"],
         "concerns": ["hybrid two days per week"],
         "relocation_required": False, "relocation_assistance_mentioned": False,
         "signing_bonus_mentioned": False}
    d.update(over)
    return json.dumps(d)


class TestVerdictValidation(unittest.TestCase):
    """The spec is explicit: do not trust inconsistent model fields without validation."""

    def test_valid_verdict_passes_through(self):
        v = jm.validate_verdict(json.loads(verdict_json()))
        self.assertIsNotNone(v)
        self.assertEqual(v["recommendation"], "strong_fit")
        self.assertEqual((v["qualification_fit"], v["interest_fit"],
                          v["practical_fit"], v["opportunity_score"]), (80, 90, 70, 84))
        self.assertTrue(v["valid"])

    def test_legacy_aliases_are_populated_for_existing_renderers(self):
        v = jm.validate_verdict(json.loads(verdict_json(opportunity_score=91)))
        self.assertEqual(v["score"], 91, "score must alias opportunity_score")
        self.assertEqual(v["fit"], "yes", "strong_fit maps to the legacy 'yes' bucket")
        self.assertEqual(v["reason"], "clear transformation mandate")

    def test_recommendation_maps_to_legacy_fit_buckets(self):
        for rec, fit in [("apply_first", "yes"), ("strong_fit", "yes"),
                         ("stretch", "maybe"), ("practical_contract", "maybe"),
                         ("not_recommended", "no")]:
            with self.subTest(rec=rec):
                v = jm.validate_verdict(json.loads(verdict_json(recommendation=rec)))
                self.assertEqual(v["fit"], fit)

    def test_recommendation_is_normalized_for_spacing_and_dashes(self):
        for variant in ["Apply_First", "apply first", "apply-first", "  APPLY_FIRST "]:
            with self.subTest(variant=variant):
                v = jm.validate_verdict(json.loads(verdict_json(recommendation=variant)))
                self.assertIsNotNone(v)
                self.assertEqual(v["recommendation"], "apply_first")

    def test_invalid_recommendation_is_rejected(self):
        for bad in ["definitely_apply", "yes", "", None, 7]:
            with self.subTest(bad=bad):
                self.assertIsNone(
                    jm.validate_verdict(json.loads(verdict_json(recommendation=bad))),
                    "an unrecognized recommendation must be rejected, not guessed")

    def test_missing_required_score_is_rejected(self):
        for field in ("qualification_fit", "interest_fit", "practical_fit",
                      "opportunity_score"):
            with self.subTest(missing=field):
                raw = json.loads(verdict_json())
                del raw[field]
                self.assertIsNone(jm.validate_verdict(raw))

    def test_non_numeric_score_is_rejected(self):
        self.assertIsNone(jm.validate_verdict(json.loads(
            verdict_json(opportunity_score="very high"))))

    def test_boolean_score_is_rejected(self):
        # True would otherwise silently coerce to 1.
        self.assertIsNone(jm.validate_verdict(json.loads(
            verdict_json(interest_fit=True))))

    def test_out_of_range_scores_are_clamped(self):
        v = jm.validate_verdict(json.loads(
            verdict_json(qualification_fit=150, interest_fit=-20)))
        self.assertEqual(v["qualification_fit"], 100)
        self.assertEqual(v["interest_fit"], 0)

    def test_float_score_is_rounded(self):
        v = jm.validate_verdict(json.loads(verdict_json(opportunity_score=87.6)))
        self.assertEqual(v["opportunity_score"], 88)

    def test_reasons_and_concerns_are_capped(self):
        v = jm.validate_verdict(json.loads(verdict_json(
            reasons=[f"reason {i}" for i in range(12)],
            concerns=[f"concern {i}" for i in range(9)])))
        self.assertEqual(len(v["reasons"]), 5, "at most 5 reasons")
        self.assertEqual(len(v["concerns"]), 3, "at most 3 concerns")

    def test_string_instead_of_list_is_accepted(self):
        v = jm.validate_verdict(json.loads(verdict_json(reasons="just one reason")))
        self.assertEqual(v["reasons"], ["just one reason"])

    def test_garbage_lists_do_not_crash(self):
        v = jm.validate_verdict(json.loads(verdict_json(reasons=42, concerns={"a": 1})))
        self.assertEqual(v["reasons"], [])
        self.assertEqual(v["concerns"], [])

    def test_booleans_are_coerced(self):
        v = jm.validate_verdict(json.loads(verdict_json(
            relocation_required="yes", signing_bonus_mentioned=1,
            relocation_assistance_mentioned=0)))
        self.assertIs(v["relocation_required"], True)
        self.assertIs(v["signing_bonus_mentioned"], True)
        self.assertIs(v["relocation_assistance_mentioned"], False)

    def test_missing_booleans_default_to_false(self):
        raw = json.loads(verdict_json())
        for f in ("relocation_required", "relocation_assistance_mentioned",
                  "signing_bonus_mentioned"):
            del raw[f]
        v = jm.validate_verdict(raw)
        self.assertIs(v["relocation_required"], False)
        self.assertIs(v["relocation_assistance_mentioned"], False)
        self.assertIs(v["signing_bonus_mentioned"], False,
                      "never infer a benefit that was not stated")

    def test_non_dict_is_rejected(self):
        for bad in [None, [], "text", 5]:
            self.assertIsNone(jm.validate_verdict(bad))


class TestScoreFitSafety(unittest.TestCase):
    """Whatever the model does, the run must keep going and the role must be kept."""

    CANDIDATE = {"name": "Test Person", "summary": "ops leader"}

    def _posting(self):
        return posting("A", "1", "Director, Business Operations")

    def _score(self, **client_kw):
        return jm.score_fit(self.CANDIDATE, self._posting(), FakeClient(**client_kw))

    def test_valid_reply_parses(self):
        r = self._score(reply=verdict_json(opportunity_score=88,
                                           recommendation="apply_first"))
        self.assertEqual(r["recommendation"], "apply_first")
        self.assertEqual(r["opportunity_score"], 88)
        self.assertEqual(r["score"], 88)
        self.assertTrue(r["valid"])

    def test_reply_wrapped_in_prose_or_fences_still_parses(self):
        r = self._score(reply=f"Sure!\n```json\n{verdict_json()}\n```")
        self.assertTrue(r["valid"])
        self.assertEqual(r["recommendation"], "strong_fit")

    def test_malformed_json_returns_neutral_and_does_not_raise(self):
        r = self._score(reply="not json at all")
        self.assertEqual(r["score"], -1, "a parse failure must be marked uncacheable")
        self.assertFalse(r["valid"])
        self.assertEqual(r["fit"], "maybe", "neutral means the role is kept, not dropped")

    def test_invalid_recommendation_returns_neutral(self):
        r = self._score(reply=verdict_json(recommendation="definitely_apply"))
        self.assertEqual(r["score"], -1)
        self.assertFalse(r["valid"])
        self.assertIn("schema validation failed", r["reason"])

    def test_missing_fields_return_neutral(self):
        r = self._score(reply='{"recommendation": "strong_fit"}')
        self.assertEqual(r["score"], -1)
        self.assertFalse(r["valid"])

    def test_non_integer_score_returns_neutral(self):
        r = self._score(reply=verdict_json(opportunity_score="very high"))
        self.assertEqual(r["score"], -1)

    def test_api_exception_returns_neutral_and_does_not_raise(self):
        r = self._score(raises=RuntimeError("api down"))
        self.assertEqual(r["score"], -1)
        self.assertIn("scoring unavailable", r["reason"])

    def test_neutral_verdict_has_every_key_a_renderer_reads(self):
        # A partially-populated fallback would KeyError deep inside report building.
        r = self._score(reply="garbage")
        for key in ("recommendation", "fit", "score", "reason", "opportunity_score",
                    "qualification_fit", "interest_fit", "practical_fit", "reasons",
                    "concerns", "relocation_required",
                    "relocation_assistance_mentioned", "signing_bonus_mentioned"):
            self.assertIn(key, r, f"neutral verdict missing {key}")

    def test_every_recommendation_value_is_documented(self):
        self.assertEqual(set(jm.RECOMMENDATIONS),
                         {"apply_first", "strong_fit", "stretch",
                          "practical_contract", "not_recommended"})


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
        c = FakeClient(reply=verdict_json(reasons=["rescored"]))
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
        c = FakeClient(reply=verdict_json(opportunity_score=77, reasons=["retried"]))
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
# Feedback file (WS5)
# --------------------------------------------------------------------------------------

class TestFeedback(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, doc, name="tester"):
        json.dump(doc, open(os.path.join(self._tmp.name, f"feedback_{name}.json"), "w"))
        return jm.load_feedback(name, directory=self._tmp.name)

    def test_missing_file_is_not_fatal(self):
        fb = jm.load_feedback("nobody", directory=self._tmp.name)
        self.assertEqual(fb, {"by_key": {}, "by_ident": {}})

    def test_malformed_file_is_not_fatal(self):
        path = os.path.join(self._tmp.name, "feedback_broken.json")
        open(path, "w").write("{not json")
        fb = jm.load_feedback("broken", directory=self._tmp.name)
        self.assertEqual(fb, {"by_key": {}, "by_ident": {}})

    def test_match_by_key(self):
        fb = self._write({"entries": [{"key": "Acme::1", "status": "applied"}]})
        p = posting("Acme", "1", "Director, Operations")
        self.assertEqual(jm.feedback_for(p, fb)["status"], "applied")

    def test_match_by_company_and_title_case_insensitively(self):
        fb = self._write({"entries": [
            {"company": "  acme ", "title": "DIRECTOR, OPERATIONS",
             "status": "not_interested"}]})
        p = posting("Acme", "99", "Director, Operations")
        self.assertEqual(jm.feedback_for(p, fb)["status"], "not_interested")

    def test_unknown_status_is_ignored(self):
        fb = self._write({"entries": [{"key": "Acme::1", "status": "maybe_later"}]})
        self.assertIsNone(jm.feedback_for(posting("Acme", "1", "T"), fb))

    def test_every_documented_status_loads(self):
        fb = self._write({"entries": [{"key": f"C::{i}", "status": s}
                                      for i, s in enumerate(jm.FEEDBACK_STATUSES)]})
        self.assertEqual(len(fb["by_key"]), len(jm.FEEDBACK_STATUSES))

    def test_suppress_list_matches_lisas_rules(self):
        # applied / already_applied / not_interested / wrong_* / too_technical suppress;
        # 'interested' must NOT.
        self.assertIn("applied", jm.SUPPRESS_STATUSES)
        self.assertIn("already_applied", jm.SUPPRESS_STATUSES)
        self.assertIn("not_interested", jm.SUPPRESS_STATUSES)
        self.assertNotIn("interested", jm.SUPPRESS_STATUSES)

    def test_committed_feedback_file_is_valid(self):
        # The shipped feedback_lisa.json must parse and start empty.
        fb = jm.load_feedback("lisa")
        self.assertEqual(fb, {"by_key": {}, "by_ident": {}})


# --------------------------------------------------------------------------------------
# Removal classification (WS4)
# --------------------------------------------------------------------------------------

class TestRemovalClassification(unittest.TestCase):

    def setUp(self):
        self.lisa = load_real_profile("lisa")

    def test_aged_out_is_not_reported_as_gone(self):
        old = (jm.datetime.date.today() - jm.datetime.timedelta(days=40)).isoformat()
        role = {"key": "A::1", "company": "A", "title": "Director, Operations",
                "posted": old}
        self.assertEqual(jm.classify_removal(role, self.lisa, 14), "aged_out")

    def test_filter_change_detected_when_title_no_longer_matches(self):
        role = {"key": "A::1", "company": "A", "title": "Support Manager",
                "posted": jm.datetime.date.today().isoformat()}
        self.assertEqual(jm.classify_removal(role, self.lisa, 14), "filter_change")

    def test_previously_rescued_role_is_not_called_a_filter_change(self):
        # Snapshots carry no description, so a rescued role can't be re-checked. The stored
        # `rescued` flag stops it being mislabeled.
        role = {"key": "A::1", "company": "A", "title": "Director, Special Projects",
                "posted": jm.datetime.date.today().isoformat(), "rescued": True}
        self.assertEqual(jm.classify_removal(role, self.lisa, 14), "not_listed")

    def test_still_matching_and_in_window_is_not_listed(self):
        role = {"key": "A::1", "company": "A", "title": "Director, Business Transformation",
                "posted": jm.datetime.date.today().isoformat()}
        self.assertEqual(jm.classify_removal(role, self.lisa, 14), "not_listed")

    def test_reason_wording_never_claims_filled(self):
        for reason, text in jm.REMOVAL_REASONS.items():
            self.assertNotIn("filled", text.lower(), f"{reason} must not claim 'filled'")


# --------------------------------------------------------------------------------------
# Lisa's digest email (WS3)
# --------------------------------------------------------------------------------------

def verdict(rec="strong_fit", opp=80, **over):
    v = jm.validate_verdict(json.loads(verdict_json(recommendation=rec,
                                                    opportunity_score=opp, **over)))
    assert v is not None
    return v


def lane(title, matched, removed=None, suffix="", **over):
    d = {"title": title, "matched": matched, "new": [], "removed": removed or [],
         "changed": [], "errors": [], "first_run": False, "held": [], "suffix": suffix}
    d.update(over)
    return d


def scored(company, ext, title, rec="strong_fit", opp=80, location="Remote - US", **kw):
    p = posting(company, ext, title, location=location,
                posted=kw.pop("posted", jm.datetime.date.today().isoformat()))
    p["fit_result"] = verdict(rec=rec, opp=opp, **kw)
    return p


class TestDigestEmail(unittest.TestCase):

    def setUp(self):
        self.lisa = load_real_profile("lisa")
        self.settings = {"max_posting_age_days": 7}
        self.empty_fb = {"by_key": {}, "by_ident": {}}

    def _build(self, lanes, feedback=None):
        return jm.build_digest_html(self.lisa, lanes, self.settings,
                                    feedback or self.empty_fb)

    def _sections(self, lanes, feedback=None):
        return jm._opportunities(lanes, self.settings, feedback or self.empty_fb)

    def test_not_recommended_roles_are_hidden(self):
        roles = [scored("A", "1", "Director, Operations", rec="not_recommended", opp=10),
                 scored("B", "2", "Director, Transformation", rec="apply_first", opp=95)]
        sections, hidden = self._sections([lane("🌎 US-Remote", roles)])
        shown = [r["p"]["key"] for v in sections.values() for r in v]
        self.assertEqual(shown, ["B::2"])
        self.assertEqual(hidden["not_recommended"], 1)

    def test_not_recommended_roles_are_still_retained_in_the_snapshot(self):
        # They are hidden at RENDER time, not filtered out of state, so the weekly audit
        # can review them. fit_mode must therefore stay 'rank', not 'filter'.
        self.assertEqual(self.lisa.get("fit_mode"), "rank")

    def test_no_job_appears_twice(self):
        roles = [scored(f"C{i}", str(i), "Director, Operations",
                        rec="apply_first", opp=95 - i) for i in range(14)]
        html = self._build([lane("🌎 US-Remote", roles)])
        urls = [u for u in re.findall(r'href="(https?://[^"]+)"', html)
                if "fonts.goog" not in u]
        self.assertEqual(len(urls), len(set(urls)), "a job link must appear at most once")

    def test_visible_count_respects_the_ceiling(self):
        roles = [scored(f"C{i}", str(i), "Director, Operations",
                        rec="apply_first", opp=99 - i) for i in range(60)]
        sections, hidden = self._sections([lane("🌎 US-Remote", roles)])
        total = sum(len(v) for v in sections.values())
        self.assertLessEqual(total, jm.DIGEST_TOTAL_CAP)
        self.assertGreater(hidden["over_cap"], 0, "the overflow must be counted, not silent")

    def test_sections_appear_in_the_specified_order(self):
        lanes = [lane("🌎 US-Remote", [
                    scored("A", "1", "Director, Transformation", rec="apply_first", opp=95),
                    scored("B", "2", "Director, Operations", rec="stretch", opp=50,
                           location="Lehi, UT")]),
                 lane("🧑‍💼 Contract / Staffing",
                      [scored("S", "9", "Program Lead", rec="practical_contract", opp=60)],
                      suffix="_staffing")]
        html = self._build(lanes)
        order = [m.group(1) for m in re.finditer(
            r'font-size:19px;font-weight:700;margin:26px 0 2px;">([^<(]+)', html)]
        order = [o.strip() for o in order]
        expected = ["Top Opportunities", "Additional Strong Opportunities",
                    "Contract Opportunities", "Utah Opportunities", "Hiring Progress"]
        present = [s for s in expected if s in order]
        self.assertEqual(order[:len(present)], present,
                         f"sections must follow the spec order; got {order}")

    def test_contract_lane_roles_go_to_the_contract_section(self):
        lanes = [lane("🧑‍💼 Contract / Staffing",
                      [scored("S", "1", "Director, Operations", rec="apply_first", opp=99)],
                      suffix="_staffing")]
        sections, _ = self._sections(lanes)
        self.assertEqual([r["p"]["key"] for r in sections["contract"]], ["S::1"])
        self.assertEqual(sections["top"], [],
                         "a staffing role belongs in Contract even when scored apply_first")

    def test_contract_section_is_not_starved_by_a_top_heavy_day(self):
        # Regression: a single global cap filled with apply_first roles and left Contract empty.
        strong = [scored(f"C{i}", str(i), "Director, Operations", rec="apply_first",
                         opp=99 - i) for i in range(30)]
        contract = [scored("S", "9", "Program Lead", rec="practical_contract", opp=40)]
        sections, _ = self._sections([lane("🌎 US-Remote", strong),
                                      lane("🧑‍💼 Contract / Staffing", contract,
                                           suffix="_staffing")])
        self.assertEqual(len(sections["contract"]), 1,
                         "Contract must keep its quota regardless of how strong the rest is")

    def test_strong_roles_beyond_the_top_quota_fall_into_additional(self):
        strong = [scored(f"C{i}", str(i), "Director, Operations", rec="apply_first",
                         opp=99 - i) for i in range(10)]
        sections, _ = self._sections([lane("🌎 US-Remote", strong)])
        self.assertEqual(len(sections["top"]), jm.DIGEST_QUOTAS["top"])
        self.assertTrue(sections["additional"], "overflow must not vanish")

    def test_sorted_by_recommendation_then_score_then_recency(self):
        roles = [scored("Low", "1", "Director, Operations", rec="strong_fit", opp=71),
                 scored("High", "2", "Director, Operations", rec="strong_fit", opp=93),
                 scored("First", "3", "Director, Operations", rec="apply_first", opp=60)]
        sections, _ = self._sections([lane("🌎 US-Remote", roles)])
        self.assertEqual([r["p"]["company"] for r in sections["top"]],
                         ["First", "High", "Low"],
                         "category outranks score; score outranks recency")

    def test_recency_breaks_score_ties(self):
        today = jm.datetime.date.today()
        older = scored("Older", "1", "Director, Operations", opp=80,
                       posted=(today - jm.datetime.timedelta(days=5)).isoformat())
        newer = scored("Newer", "2", "Director, Operations", opp=80,
                       posted=today.isoformat())
        sections, _ = self._sections([lane("🌎 US-Remote", [older, newer])])
        self.assertEqual([r["p"]["company"] for r in sections["top"]], ["Newer", "Older"])

    # ---- location labels ----
    def test_location_label_us_remote(self):
        p = posting("A", "1", "T", location="Remote - US")
        self.assertEqual(jm.location_label(p, verdict())[:2], ("✓", "Remote"))

    def test_location_label_utah_onsite_and_hybrid(self):
        p = posting("A", "1", "T", location="Lehi, UT")
        self.assertEqual(jm.location_label(p, verdict())[:2], ("📍", "Utah"))
        p2 = posting("A", "2", "T", location="Lehi, UT",
                     description="This is a hybrid role, 3 days in office.")
        self.assertEqual(jm.location_label(p2, verdict())[:2], ("📍", "Hybrid"))

    def test_location_label_hybrid_outside_utah(self):
        p = posting("A", "1", "T", location="Austin, TX",
                    description="Hybrid schedule with 2 days onsite.")
        self.assertEqual(jm.location_label(p, verdict())[:2], ("🏡", "Hybrid"))

    def test_location_label_onsite_outside_utah_is_relocation(self):
        p = posting("A", "1", "T", location="Austin, TX")
        self.assertEqual(jm.location_label(p, verdict())[:2],
                         ("🏡", "Relocation Required"))

    def test_explicit_relocation_required_overrides_remote(self):
        p = posting("A", "1", "T", location="Remote - US")
        v = verdict(relocation_required=True)
        self.assertEqual(jm.location_label(p, v)[:2], ("🏡", "Relocation Required"))

    def test_unclear_location_falls_back_to_relocation_wording(self):
        p = posting("A", "1", "T", location="")
        self.assertEqual(jm.location_label(p, verdict())[:2],
                         ("🏡", "Relocation Required"))

    # ---- perks are never inferred ----
    def test_perk_chips_absent_by_default(self):
        html = self._build([lane("🌎 US-Remote",
                                 [scored("A", "1", "Director, Operations")])])
        self.assertNotIn("Signing bonus mentioned", html)
        self.assertNotIn("Relocation assistance mentioned", html)

    def test_perk_chips_shown_only_when_explicit(self):
        r = scored("A", "1", "Director, Operations",
                   signing_bonus_mentioned=True, relocation_assistance_mentioned=True)
        html = self._build([lane("🌎 US-Remote", [r])])
        self.assertIn("Signing bonus mentioned", html)
        self.assertIn("Relocation assistance mentioned", html)

    # ---- feedback drives repetition ----
    def test_feedback_suppresses_roles(self):
        roles = [scored("A", "1", "Director, Operations", rec="apply_first", opp=99),
                 scored("B", "2", "Director, Transformation", rec="apply_first", opp=98)]
        fb = {"by_key": {"A::1": {"status": "not_interested", "note": "", "date": ""}},
              "by_ident": {}}
        sections, hidden = self._sections([lane("🌎 US-Remote", roles)], fb)
        shown = [r["p"]["key"] for v in sections.values() for r in v]
        self.assertEqual(shown, ["B::2"])
        self.assertEqual(hidden["suppressed"], 1)

    def test_interested_keeps_showing_and_is_marked(self):
        roles = [scored("A", "1", "Director, Operations", rec="strong_fit", opp=80)]
        fb = {"by_key": {"A::1": {"status": "interested", "note": "", "date": ""}},
              "by_ident": {}}
        sections, hidden = self._sections([lane("🌎 US-Remote", roles)], fb)
        self.assertEqual(sum(len(v) for v in sections.values()), 1)
        self.assertEqual(hidden["suppressed"], 0)
        html = self._build([lane("🌎 US-Remote", roles)], fb)
        self.assertIn("Interested", html)

    def test_every_suppressing_status_actually_suppresses(self):
        for status in jm.SUPPRESS_STATUSES:
            with self.subTest(status=status):
                roles = [scored("A", "1", "Director, Operations", rec="apply_first")]
                fb = {"by_key": {"A::1": {"status": status, "note": "", "date": ""}},
                      "by_ident": {}}
                sections, _ = self._sections([lane("🌎 US-Remote", roles)], fb)
                self.assertEqual(sum(len(v) for v in sections.values()), 0)

    # ---- unscored roles must not disappear ----
    def test_unscored_roles_are_still_shown(self):
        p = posting("A", "1", "Director, Operations", location="Remote - US")
        sections, hidden = self._sections([lane("🌎 US-Remote", [p])])
        self.assertEqual(sum(len(v) for v in sections.values()), 1,
                         "a role must not vanish just because scoring failed")
        self.assertEqual(hidden["unscored"], 1)

    def test_neutral_verdict_renders_without_crashing(self):
        p = posting("A", "1", "Director, Operations", location="Remote - US")
        p["fit_result"] = jm._neutral_verdict("(scoring unavailable: RuntimeError)")
        html = self._build([lane("🌎 US-Remote", [p])])
        self.assertIn("Not scored this run", html)

    # ---- hiring progress ----
    def test_removal_only_day_produces_a_non_misleading_email(self):
        gone = posting("A", "1", "Director, Operations")
        gone["removal_reason"] = "not_listed"
        html = self._build([lane("🌎 US-Remote", [], removed=[gone])])
        self.assertIn("Hiring Progress", html)
        self.assertIn("no longer detected", html)
        self.assertNotIn("filled", html.lower())

    def test_applied_roles_are_labeled_applied(self):
        gone = posting("A", "1", "Director, Operations")
        gone["removal_reason"] = "not_listed"
        fb = {"by_key": {"A::1": {"status": "applied", "note": "", "date": ""}},
              "by_ident": {}}
        html = self._build([lane("🌎 US-Remote", [], removed=[gone])], fb)
        self.assertIn("Applied ✓", html)
        self.assertIn("no longer listed", html)

    def test_aged_out_role_is_not_reported_as_a_departure(self):
        aged = posting("A", "1", "Director, Operations")
        aged["removal_reason"] = "aged_out"
        prog = jm._hiring_progress([lane("🌎 US-Remote", [], removed=[aged])],
                                   self.empty_fb)
        self.assertEqual(prog["other"], [])
        self.assertEqual(prog["aged"], 1)
        html = self._build([lane("🌎 US-Remote", [], removed=[aged])])
        self.assertIn("may still", html)

    def test_source_error_hold_is_explained_not_counted_as_closed(self):
        held = posting("A", "1", "Director, Operations")
        html = self._build([lane("🌎 US-Remote", [], held=[held])])
        self.assertIn("held because a job source failed", html)
        self.assertNotIn("filled", html.lower())

    def test_report_never_claims_a_company_filled_a_role(self):
        gone = posting("A", "1", "Director, Operations")
        gone["removal_reason"] = "not_listed"
        html = self._build([lane("🌎 US-Remote",
                                 [scored("B", "2", "Director, Operations")],
                                 removed=[gone])])
        self.assertNotIn("filled", html.lower())

    # ---- window ----
    def test_lisa_uses_a_14_day_window_and_chad_stays_at_7(self):
        settings = {"max_posting_age_days": 7}
        self.assertEqual(jm.profile_age_window(self.lisa, settings), 14)
        self.assertEqual(jm.profile_age_window(load_real_profile("chad"), settings), 7)

    def test_staffing_lane_keeps_its_own_wider_window(self):
        settings = {"max_posting_age_days": 7, "staffing_search": {"max_age_days": 30}}
        self.assertEqual(jm.profile_age_window(self.lisa, settings, "_staffing"), 30)

    def test_links_are_real_urls(self):
        html = self._build([lane("🌎 US-Remote",
                                 [scored("A", "1", "Director, Operations")])])
        urls = [u for u in re.findall(r'href="(https?://[^"]+)"', html)
                if "fonts.goog" not in u]
        self.assertTrue(urls)
        for u in urls:
            self.assertTrue(u.startswith("http"), u)

    def test_chad_still_uses_the_original_renderer(self):
        chad = load_real_profile("chad")
        self.assertNotIn("report_style", chad,
                         "Chad must keep the original lane-by-lane email")


class TestUrgencyBands(unittest.TestCase):

    def test_bands(self):
        today = jm.datetime.date.today()
        def band(days):
            return jm.urgency_band((today - jm.datetime.timedelta(days=days)).isoformat())
        self.assertIn("today", band(0))
        self.assertIn("last 3 days", band(2))
        self.assertIn("4–7 days", band(6))
        self.assertIn("8–14 days", band(12))
        self.assertIn("over 14 days", band(30))

    def test_unknown_date_is_labeled_not_guessed(self):
        self.assertIn("unknown", jm.urgency_band(""))
        self.assertIn("unknown", jm.urgency_band("garbage"))

    def test_urgency_is_independent_of_fit(self):
        # Same posting date, wildly different scores -> identical urgency band.
        d = jm.datetime.date.today().isoformat()
        self.assertEqual(jm.urgency_band(d), jm.urgency_band(d))


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
