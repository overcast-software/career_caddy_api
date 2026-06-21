"""Tests for GET /api/v1/scrape-profiles/extension-selectors/?hostname=.

The browser extension fetches per-host selectors via this endpoint on
every send so api becomes the single source of truth for what to scrape
in-page. The rest of ScrapeProfileViewSet is staff-only — this action
overrides that so any authenticated user (the extension's `jh_*` API
key holder) can read the safe selector subset.
"""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import ScrapeProfile

User = get_user_model()


class TestScrapeProfileExtensionSelectors(TestCase):
    URL = "/api/v1/scrape-profiles/extension-selectors/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ext", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_returns_linkedin_seeded_selectors(self):
        """0076 data migration seeds linkedin.com, 0077 refreshes the
        apply-button selectors to track LinkedIn's current DOM. Verify
        the shape the extension's DECODERS registry expects."""
        resp = self.client.get(self.URL + "?hostname=linkedin.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["hostname"], "linkedin.com")
        # aria-label is durable across LinkedIn's atomic-CSS rotations;
        # the legacy `a.jobs-apply-button` class no longer appears on
        # the rendered apply button.
        self.assertIn(
            'a[aria-label="Apply on company website"][href]',
            attrs["apply_button_selectors"],
        )
        self.assertIn(
            'meta[property="og:url"]', attrs["canonical_link_selectors"]
        )
        self.assertEqual(attrs["apply_url_decoder"], "linkedin_safety_go")

    def test_subdomain_inherits_parent_profile(self):
        """jobs.linkedin.com should resolve to the linkedin.com profile —
        single row covers a host family, no per-subdomain duplication."""
        resp = self.client.get(self.URL + "?hostname=jobs.linkedin.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.json()["data"]["attributes"]["hostname"], "linkedin.com"
        )

    def test_www_prefix_stripped(self):
        """www.linkedin.com normalizes to linkedin.com via the same
        prefix-strip rule used everywhere else in the codebase."""
        resp = self.client.get(self.URL + "?hostname=www.linkedin.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.json()["data"]["attributes"]["hostname"], "linkedin.com"
        )

    def test_unknown_host_returns_404(self):
        """No profile for the host → 404; the extension falls back to
        its baked defaults."""
        resp = self.client.get(self.URL + "?hostname=jobs.example.com")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_profile_without_extension_selectors_returns_404(self):
        """A ScrapeProfile may exist for a host but have no extension
        selectors configured — same outcome as no profile at all."""
        ScrapeProfile.objects.create(hostname="noselectors.com")
        resp = self.client.get(self.URL + "?hostname=noselectors.com")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_job_data_only_profile_returns_200(self):
        """A profile that ships css_selectors.job_data but no
        extension_selectors block should still return — the structured-
        prefill path needs the job_data selectors and doesn't depend on
        the apply/canonical bundle."""
        ScrapeProfile.objects.create(
            hostname="jobdataonly.com",
            css_selectors={"job_data": {"title": "h1"}},
        )
        resp = self.client.get(self.URL + "?hostname=jobdataonly.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["job_data_selectors"], {"title": "h1"})
        self.assertEqual(attrs["apply_button_selectors"], [])

    def test_response_carries_known_good_tier_and_reasons(self):
        """The response root carries the readiness signal so the staff
        extension panel can show WHY a profile is/isn't known-good without a
        second round-trip. A fresh job_data-only profile fails several
        readiness clauses, so `known_good` is False and `reasons` lists them
        (e.g. the missing required job_data selectors)."""
        ScrapeProfile.objects.create(
            hostname="reasons.com",
            css_selectors={"job_data": {"title": "h1"}},
        )
        resp = self.client.get(self.URL + "?hostname=reasons.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertIn("known_good", body)
        self.assertIn("tier", body)
        self.assertIn("reasons", body)
        self.assertFalse(body["known_good"])
        self.assertIsInstance(body["reasons"], list)
        self.assertTrue(body["reasons"])  # fresh profile fails clauses
        self.assertIn("job_data selectors", " ".join(body["reasons"]))

    def test_linkedin_response_includes_job_data_selectors(self):
        """0093 seeds linkedin.com with title + company_name job_data
        selectors; verify they round-trip through the endpoint so the
        extension can run them client-side."""
        resp = self.client.get(self.URL + "?hostname=linkedin.com")
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("job_data_selectors", attrs)
        self.assertEqual(attrs["job_data_selectors"]["title"], "h1")
        self.assertIn(
            "/company/", attrs["job_data_selectors"]["company_name"]
        )

    def test_missing_hostname_param_returns_400(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_profile_falls_through_to_parent(self):
        """A disabled subdomain profile shouldn't block the parent
        match — the extension still gets the parent's selectors."""
        ScrapeProfile.objects.create(
            hostname="jobs.linkedin.com",
            extension_selectors={"apply_button_selectors": ["a.disabled"]},
            enabled=False,
        )
        resp = self.client.get(self.URL + "?hostname=jobs.linkedin.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.json()["data"]["attributes"]["hostname"], "linkedin.com"
        )

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(self.URL + "?hostname=linkedin.com")
        self.assertIn(resp.status_code, (401, 403))

    def test_non_staff_user_allowed(self):
        """The action overrides the viewset's IsAdminUser gate so
        ordinary users (the extension's API key holder) can call it."""
        self.assertFalse(self.user.is_staff)
        resp = self.client.get(self.URL + "?hostname=linkedin.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
