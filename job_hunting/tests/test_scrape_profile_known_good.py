"""Tests for the per-domain known-good signal on ScrapeProfile.

Covers the public contract three downstream consumers build against
(the email auto-scrape poller in automation/, the browser extension in
frontend/, the scrape graph in agents/):

- ``ScrapeProfile.is_known_good`` @property + ``readiness()`` debug struct
  (truth-table across every clause).
- The serializer exposing ``is_known_good`` (+ ``readiness``) on the wire.
- ``GET /scrape-profiles/extension-selectors/`` adding top-level
  ``known_good`` + ``tier`` without breaking the existing bundle.
- Promotion: driving ``_update_scrape_profile`` outcomes flips a fresh
  profile to known-good; sustained tier-0 misses demote it back.
"""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import ScrapeProfile, Scrape
from job_hunting.models.scrape_profile import (
    KNOWN_GOOD_MIN_SCRAPE_COUNT,
    KNOWN_GOOD_MIN_SUCCESS_RATE,
)
from job_hunting.lib.parsers.job_post_extractor import _update_scrape_profile

User = get_user_model()

# A fully-populated job_data selector blob (all required fields present).
GOOD_JOB_DATA = {"title": "h1", "company_name": ".company", "description": "#desc"}


def make_profile(hostname="known-good.com", **overrides):
    """Build a baseline *known-good* profile; override one field to break it."""
    kwargs = dict(
        hostname=hostname,
        enabled=True,
        css_selectors={"job_data": dict(GOOD_JOB_DATA)},
        preferred_tier="auto",
        success_rate=0.9,
        scrape_count=5,
        tier0_miss_count=1,  # ratio 0.2 < 0.5
    )
    kwargs.update(overrides)
    return ScrapeProfile.objects.create(**kwargs)


class TestIsKnownGoodTruthTable(TestCase):
    def test_baseline_is_known_good(self):
        prof = make_profile()
        self.assertTrue(prof.is_known_good)
        r = prof.readiness()
        self.assertEqual(r["known_good"], True)
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["tier"], "auto")

    def test_disabled_blocks(self):
        prof = make_profile(enabled=False)
        self.assertFalse(prof.is_known_good)
        self.assertIn("disabled", prof.readiness()["reasons"])

    def test_missing_description_selector_blocks(self):
        prof = make_profile(
            css_selectors={"job_data": {"title": "h1", "company_name": ".c"}}
        )
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("description" in r for r in prof.readiness()["reasons"])
        )

    def test_missing_company_name_selector_blocks(self):
        prof = make_profile(
            css_selectors={"job_data": {"title": "h1", "description": "#d"}}
        )
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("company_name" in r for r in prof.readiness()["reasons"])
        )

    def test_empty_string_selector_counts_as_missing(self):
        prof = make_profile(
            css_selectors={
                "job_data": {"title": "h1", "company_name": "  ", "description": "#d"}
            }
        )
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("company_name" in r for r in prof.readiness()["reasons"])
        )

    def test_null_css_selectors_blocks(self):
        prof = make_profile(css_selectors=None)
        self.assertFalse(prof.is_known_good)

    def test_missing_job_data_key_blocks(self):
        prof = make_profile(css_selectors={"apply": {"x": "y"}})
        self.assertFalse(prof.is_known_good)

    def test_low_success_rate_blocks(self):
        prof = make_profile(success_rate=0.5)
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("success_rate" in r for r in prof.readiness()["reasons"])
        )

    def test_success_rate_at_threshold_passes(self):
        prof = make_profile(success_rate=KNOWN_GOOD_MIN_SUCCESS_RATE)
        self.assertTrue(prof.is_known_good)

    def test_low_scrape_count_blocks(self):
        prof = make_profile(scrape_count=2, tier0_miss_count=0)
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("scrape_count" in r for r in prof.readiness()["reasons"])
        )

    def test_scrape_count_at_threshold_passes(self):
        prof = make_profile(
            scrape_count=KNOWN_GOOD_MIN_SCRAPE_COUNT, tier0_miss_count=0
        )
        self.assertTrue(prof.is_known_good)

    def test_high_miss_ratio_blocks(self):
        # 3 misses / 5 scrapes = 0.6 >= 0.5
        prof = make_profile(scrape_count=5, tier0_miss_count=3)
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("tier0_miss_ratio" in r for r in prof.readiness()["reasons"])
        )

    def test_miss_ratio_exactly_threshold_blocks(self):
        # 2 / 4 = 0.5 → NOT strictly below threshold → blocked
        prof = make_profile(scrape_count=4, tier0_miss_count=2)
        self.assertFalse(prof.is_known_good)

    def test_wrong_tier_blocks(self):
        prof = make_profile(preferred_tier="1")
        self.assertFalse(prof.is_known_good)
        self.assertTrue(
            any("preferred_tier" in r for r in prof.readiness()["reasons"])
        )

    def test_tier_zero_passes(self):
        prof = make_profile(preferred_tier="0")
        self.assertTrue(prof.is_known_good)
        self.assertEqual(prof.readiness()["tier"], "0")

    def test_multiple_failing_clauses_all_listed(self):
        prof = make_profile(
            enabled=False, success_rate=0.1, scrape_count=0, css_selectors=None
        )
        reasons = prof.readiness()["reasons"]
        self.assertFalse(prof.is_known_good)
        self.assertGreaterEqual(len(reasons), 4)


class TestEffectiveTier(TestCase):
    def test_tier_zero_maps_to_zero(self):
        self.assertEqual(make_profile(preferred_tier="0").effective_tier, "0")

    def test_auto_surfaces_as_auto(self):
        self.assertEqual(make_profile(preferred_tier="auto").effective_tier, "auto")

    def test_demoted_tier_surfaces_verbatim(self):
        self.assertEqual(make_profile(preferred_tier="2").effective_tier, "2")


class TestSerializerExposesSignal(TestCase):
    URL = "/api/v1/scrape-profiles/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="admin-kg", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.user)

    def test_retrieve_includes_is_known_good_and_readiness(self):
        prof = make_profile()
        resp = self.client.get(f"{self.URL}{prof.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        # Wire key is snake_case (this codebase does NOT dasherize).
        self.assertIn("is_known_good", attrs)
        self.assertIs(attrs["is_known_good"], True)
        self.assertIn("readiness", attrs)
        self.assertEqual(attrs["readiness"]["known_good"], True)
        self.assertEqual(attrs["readiness"]["tier"], "auto")
        self.assertEqual(attrs["readiness"]["reasons"], [])

    def test_list_includes_is_known_good(self):
        make_profile(hostname="kg-list.com")
        resp = self.client.get(f"{self.URL}?filter[hostname]=kg-list.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertIn("is_known_good", attrs)

    def test_readiness_opt_out_via_sparse_fieldset(self):
        prof = make_profile(hostname="kg-fields.com")
        resp = self.client.get(
            f"{self.URL}{prof.id}/?fields[scrape-profile]=is_known_good"
        )
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("is_known_good", attrs)
        self.assertNotIn("readiness", attrs)

    def test_is_known_good_dropped_on_inbound_patch(self):
        # Read-only: a client cannot fabricate the signal via PATCH.
        prof = make_profile(hostname="kg-ro.com", success_rate=0.1, scrape_count=0)
        self.assertFalse(prof.is_known_good)
        resp = self.client.patch(
            f"{self.URL}{prof.id}/",
            {
                "data": {
                    "type": "scrape-profile",
                    "id": str(prof.id),
                    "attributes": {"is_known_good": True, "is-known-good": True},
                }
            },
            format="json",
        )
        self.assertIn(resp.status_code, (200, 202))
        prof.refresh_from_db()
        self.assertFalse(prof.is_known_good)


class TestExtensionSelectorsKnownGood(TestCase):
    URL = "/api/v1/scrape-profiles/extension-selectors/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ext-kg", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_known_good_profile_reports_true_and_tier(self):
        ScrapeProfile.objects.create(
            hostname="kg-ext.com",
            enabled=True,
            css_selectors={"job_data": dict(GOOD_JOB_DATA)},
            extension_selectors={"apply_button_selectors": ["a.apply"]},
            preferred_tier="0",
            success_rate=0.95,
            scrape_count=8,
            tier0_miss_count=0,
        )
        resp = self.client.get(self.URL + "?hostname=kg-ext.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        # New top-level keys.
        self.assertIs(body["known_good"], True)
        self.assertEqual(body["tier"], "0")
        # Existing bundle untouched.
        self.assertEqual(body["data"]["attributes"]["hostname"], "kg-ext.com")
        self.assertEqual(
            body["data"]["attributes"]["apply_button_selectors"], ["a.apply"]
        )
        self.assertEqual(
            body["data"]["attributes"]["job_data_selectors"], GOOD_JOB_DATA
        )

    def test_not_yet_known_good_profile_reports_false_with_tier(self):
        # Extension selectors present but extraction metrics immature.
        ScrapeProfile.objects.create(
            hostname="immature-ext.com",
            enabled=True,
            extension_selectors={"apply_button_selectors": ["a.apply"]},
            preferred_tier="auto",
            success_rate=0.0,
            scrape_count=0,
        )
        resp = self.client.get(self.URL + "?hostname=immature-ext.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertIs(body["known_good"], False)
        self.assertEqual(body["tier"], "auto")
        # Existing shape still intact.
        self.assertIn("apply_button_selectors", body["data"]["attributes"])


class TestPromotionViaOutcomes(TestCase):
    """Driving _update_scrape_profile outcomes flips the signal; demotion
    wins on sustained tier-0 misses."""

    def setUp(self):
        self.user = User.objects.create_user(username="promo", password="pw")
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="x" * 100,
            created_by=self.user,
        )

    def _seed_profile(self):
        # Selectors already discovered/graduated; extraction_hints set so
        # _update_scrape_profile skips the LLM hint-generation path.
        return ScrapeProfile.objects.create(
            hostname="example.com",
            enabled=True,
            css_selectors={"job_data": dict(GOOD_JOB_DATA)},
            extraction_hints="seeded",
            preferred_tier="auto",
            success_rate=0.0,
            scrape_count=0,
            tier0_miss_count=0,
        )

    def test_fresh_profile_flips_to_known_good_after_successes(self):
        prof = self._seed_profile()
        self.assertFalse(prof.is_known_good)  # scrape_count=0, rate=0.0
        for _ in range(KNOWN_GOOD_MIN_SCRAPE_COUNT):
            _update_scrape_profile(
                self.scrape, self.user, success=True, tier0_hit=True
            )
        prof.refresh_from_db()
        self.assertEqual(prof.scrape_count, KNOWN_GOOD_MIN_SCRAPE_COUNT)
        self.assertEqual(prof.success_rate, 1.0)
        self.assertEqual(prof.tier0_miss_count, 0)
        self.assertTrue(prof.is_known_good)

    def test_sustained_tier0_misses_demote_and_block_known_good(self):
        prof = self._seed_profile()
        prof.success_rate = 1.0
        prof.scrape_count = 1
        prof.save()
        for _ in range(6):
            _update_scrape_profile(
                self.scrape, self.user, success=True, tier0_hit=False
            )
        prof.refresh_from_db()
        self.assertEqual(prof.preferred_tier, "1")  # demoted
        self.assertFalse(prof.is_known_good)  # demotion wins
