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
