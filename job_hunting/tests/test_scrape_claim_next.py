"""Phase 1 of Plans/Scrape runner — atomic claim endpoint.

Pins the contract for POST /api/v1/scrapes/claim-next/. The runner-
side code (agents/runners/scrape_runner.py) is built against this
shape; breaking the contract here ripples to every runner instance
(omarchy, pibu, future hosts).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from job_hunting.models import Scrape


User = get_user_model()

CLAIM_URL = "/api/v1/scrapes/claim-next/"


class TestScrapeClaimNext(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="runner", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, runner_name="omarchy"):
        return self.client.post(
            CLAIM_URL, data={"runner_name": runner_name}, format="json"
        )

    def test_claims_the_only_hold_scrape(self):
        scrape = Scrape.objects.create(url="https://example.com/a", status="hold")
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(str(body["data"]["id"]), str(scrape.id))

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")
        self.assertEqual(scrape.claimed_by, "omarchy")
        self.assertIsNotNone(scrape.claimed_at)

    def test_no_hold_returns_204(self):
        # No scrapes in any state → 204.
        resp = self._post()
        self.assertEqual(resp.status_code, 204)

    def test_skips_already_claimed_rows(self):
        """A scrape with claimed_at set but still status='hold' (race
        edge case during the claim window) is skipped — the runner
        sees the next unclaimed one or 204."""
        Scrape.objects.create(
            url="https://example.com/already-claimed",
            status="hold",
            claimed_at=timezone.now(),
            claimed_by="someone-else",
        )
        Scrape.objects.create(
            url="https://example.com/available",
            status="hold",
        )

        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Should pick the unclaimed one, not the already-claimed one.
        claimed_scrape = Scrape.objects.get(pk=body["data"]["id"])
        self.assertEqual(claimed_scrape.url, "https://example.com/available")

    def test_skips_non_hold_statuses(self):
        Scrape.objects.create(url="https://example.com/done", status="completed")
        Scrape.objects.create(url="https://example.com/run", status="running")
        Scrape.objects.create(url="https://example.com/fail", status="failed")
        resp = self._post()
        self.assertEqual(resp.status_code, 204)

    def test_fifo_ordering_by_id(self):
        """First-created hold scrape gets claimed first. Pins the
        ordering so a queue of N hold rows clears in arrival order."""
        first = Scrape.objects.create(url="https://example.com/1", status="hold")
        second = Scrape.objects.create(url="https://example.com/2", status="hold")
        third = Scrape.objects.create(url="https://example.com/3", status="hold")

        for expected in (first, second, third):
            resp = self._post()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                str(resp.json()["data"]["id"]), str(expected.id)
            )

        # All consumed.
        self.assertEqual(self._post().status_code, 204)

    def test_runner_name_defaults_to_user_agent(self):
        Scrape.objects.create(url="https://example.com/ua", status="hold")
        resp = self.client.post(
            CLAIM_URL,
            data={},
            format="json",
            HTTP_USER_AGENT="cc-scrape-runner/1.0",
        )
        self.assertEqual(resp.status_code, 200)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.claimed_by, "cc-scrape-runner/1.0")

    def test_runner_name_falls_back_to_anonymous(self):
        Scrape.objects.create(url="https://example.com/anon", status="hold")
        resp = self.client.post(CLAIM_URL, data={}, format="json")
        self.assertEqual(resp.status_code, 200)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        # Test client doesn't set User-Agent by default → falls back.
        self.assertEqual(scrape.claimed_by, "anonymous")

    def test_status_transition_atomic(self):
        """The claim should flip status='hold' → 'running' in the same
        statement as setting claimed_at. Verify both land together."""
        scrape = Scrape.objects.create(url="https://example.com/x", status="hold")
        self._post()
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")
        self.assertIsNotNone(scrape.claimed_at)
        self.assertGreater(
            scrape.claimed_at, timezone.now() - timedelta(seconds=5)
        )

    def test_requires_authentication(self):
        unauth = APIClient()
        Scrape.objects.create(url="https://example.com/auth", status="hold")
        resp = unauth.post(CLAIM_URL, data={}, format="json")
        self.assertEqual(resp.status_code, 401)
