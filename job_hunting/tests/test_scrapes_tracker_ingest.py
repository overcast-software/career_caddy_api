"""Integration: POST /api/v1/scrapes/ with a tracker URL.

The tracker resolver runs before the dedupe gate so two distinct
per-recipient tracker URLs that resolve to the same destination
collapse to the same canonical link, and dead trackers (4xx/5xx)
are rejected with a 400 instead of minting a doomed scrape.
"""
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import JobPost, Scrape


User = get_user_model()


class ScrapeIngestTrackerResolutionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, url):
        body = {"data": {"attributes": {"url": url, "status": "hold"}}}
        return self.client.post("/api/v1/scrapes/", body, format="json")

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_tracker_resolves_and_stores_source_link(self, mock_head):
        resp = MagicMock()
        resp.url = "https://example.com/jobs/123"
        resp.status_code = 200
        mock_head.return_value = resp

        tracker = "https://url9751.alerts.jobot.com/ls/click?upn=abc"
        r = self._post(tracker)

        self.assertEqual(r.status_code, 201)
        scrape = Scrape.objects.get()
        self.assertEqual(scrape.url, "https://example.com/jobs/123")
        self.assertEqual(scrape.source_link, tracker)

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_dead_tracker_returns_400(self, mock_head):
        resp = MagicMock()
        resp.url = "https://example.com/expired"
        resp.status_code = 404
        mock_head.return_value = resp

        r = self._post("https://click.ziprecruiter.com/dead-link")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            r.json()["errors"][0]["code"], "tracker_unresolved",
        )
        self.assertEqual(Scrape.objects.count(), 0)

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_two_tracker_urls_resolving_to_same_destination_dedupe(self, mock_head):
        # First request lands fresh and creates a scrape (no existing
        # JobPost to dedupe against here). Second request for a
        # different tracker URL but same destination would collide
        # with the canonical_link if a JobPost existed. We test the
        # *resolver* part: both POSTs resolve to the same canonical
        # URL, so callers can dedupe downstream.
        resp = MagicMock()
        resp.url = "https://example.com/jobs/123?utm_source=mail&id=456"
        resp.status_code = 200
        mock_head.return_value = resp

        r1 = self._post("https://url9751.alerts.jobot.com/ls/click?upn=A")
        r2 = self._post("https://url9751.alerts.jobot.com/ls/click?upn=B")

        s1 = Scrape.objects.get(id=r1.json()["data"]["id"])
        s2 = Scrape.objects.get(id=r2.json()["data"]["id"])
        self.assertEqual(s1.url, "https://example.com/jobs/123?id=456")
        self.assertEqual(s2.url, "https://example.com/jobs/123?id=456")
        self.assertNotEqual(s1.source_link, s2.source_link)

    def test_non_tracker_url_skips_resolution(self):
        # No mock — if the resolver ran for a non-tracker URL the
        # request would fail (no live network in tests).
        r = self._post("https://example.com/careers/123")
        self.assertEqual(r.status_code, 201)
        scrape = Scrape.objects.get()
        self.assertEqual(scrape.url, "https://example.com/careers/123")
        self.assertIsNone(scrape.source_link)

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_tracker_resolution_runs_before_dedupe_gate(self, mock_head):
        # Existing JobPost lives at the canonical destination. A new
        # scrape for a tracker URL that resolves to that destination
        # should hit the 409 dedupe path — proving the resolver runs
        # *before* the dedupe check.
        JobPost.objects.create(
            title="Already here",
            link="https://example.com/jobs/123",
            canonical_link="https://example.com/jobs/123",
            created_by=self.user,
        )
        resp = MagicMock()
        resp.url = "https://example.com/jobs/123"
        resp.status_code = 200
        mock_head.return_value = resp

        r = self._post("https://url9751.alerts.jobot.com/ls/click?upn=Z")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["errors"][0]["code"], "duplicate")
        self.assertEqual(Scrape.objects.count(), 0)
