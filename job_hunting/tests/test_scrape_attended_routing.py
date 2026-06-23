"""Attended-scrape routing — partitioned hold claim queue.

Pins the contract for the ``Scrape.attended`` flag end to end:

- POST /api/v1/scrapes/ accepts ``attended`` in attributes and persists
  it (default False when omitted). cc_auto sends ``attended: true`` to
  route a scrape to a human-driven headed ("attended") runner.
- POST /api/v1/scrapes/claim-next/ partitions the hold queue on the
  flag: a default/unattended runner NEVER claims an attended-marked
  scrape, and an attended runner claims ONLY attended-marked scrapes.

The two queues never cross — that is the load-bearing invariant the
agents/ runner + cc_auto coordinate against. Breaking it here ripples
to every runner instance.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Scrape


User = get_user_model()

CLAIM_URL = "/api/v1/scrapes/claim-next/"
CREATE_URL = "/api/v1/scrapes/"


class TestScrapeCreateAttended(TestCase):
    """POST /api/v1/scrapes/ persists the ``attended`` attribute."""

    def setUp(self):
        # Scrape creation is staff-only during alpha.
        self.user = User.objects.create_user(
            username="staff", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _create(self, url, **attrs):
        body = {"data": {"attributes": {"url": url, "status": "hold", **attrs}}}
        return self.client.post(CREATE_URL, body, format="json")

    def test_attended_true_persists(self):
        resp = self._create("https://attended.example/jobs/1", attended=True)
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        # Writable snake_case attribute echoed back on the resource.
        self.assertIs(body["data"]["attributes"]["attended"], True)
        scrape = Scrape.objects.get(pk=int(body["data"]["id"]))
        self.assertTrue(scrape.attended)

    def test_attended_defaults_to_false_when_omitted(self):
        resp = self._create("https://attended.example/jobs/2")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIs(body["data"]["attributes"]["attended"], False)
        scrape = Scrape.objects.get(pk=int(body["data"]["id"]))
        self.assertFalse(scrape.attended)

    def test_attended_false_explicit_persists(self):
        resp = self._create("https://attended.example/jobs/3", attended=False)
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertFalse(scrape.attended)


class TestScrapeClaimNextAttendedPartition(TestCase):
    """POST /api/v1/scrapes/claim-next/ partitions on ``attended``."""

    def setUp(self):
        self.user = User.objects.create_user(username="runner", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _claim(self, **body):
        return self.client.post(CLAIM_URL, data=body, format="json")

    def test_default_claim_skips_attended_row_and_claims_unattended(self):
        """No ``attended`` param => default/unattended runner. It must
        skip an OLDER attended=True hold and claim the attended=False one,
        even though FIFO-by-id would otherwise pick the attended row."""
        attended_row = Scrape.objects.create(
            url="https://example.com/attended", status="hold", attended=True
        )
        unattended_row = Scrape.objects.create(
            url="https://example.com/unattended", status="hold", attended=False
        )

        resp = self._claim()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            str(resp.json()["data"]["id"]), str(unattended_row.id)
        )

        # The attended row is untouched — still an unclaimed hold.
        attended_row.refresh_from_db()
        self.assertEqual(attended_row.status, "hold")
        self.assertIsNone(attended_row.claimed_at)

    def test_default_claim_never_drains_attended_only_queue(self):
        """The load-bearing invariant: a default runner returns 204 when
        only attended=True holds exist — it never crosses into that
        queue, so the scrape waits for an attended runner."""
        Scrape.objects.create(
            url="https://example.com/attended-only", status="hold", attended=True
        )
        resp = self._claim()
        self.assertEqual(resp.status_code, 204)

    def test_attended_claim_skips_unattended_and_claims_attended(self):
        """``attended=True`` => attended runner. It must skip an OLDER
        attended=False hold and claim the attended=True one."""
        unattended_row = Scrape.objects.create(
            url="https://example.com/unattended", status="hold", attended=False
        )
        attended_row = Scrape.objects.create(
            url="https://example.com/attended", status="hold", attended=True
        )

        resp = self._claim(attended=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            str(resp.json()["data"]["id"]), str(attended_row.id)
        )

        unattended_row.refresh_from_db()
        self.assertEqual(unattended_row.status, "hold")
        self.assertIsNone(unattended_row.claimed_at)

    def test_attended_claim_never_drains_unattended_queue(self):
        """Symmetric invariant: an attended runner returns 204 when only
        attended=False holds exist — it never claims the default queue."""
        Scrape.objects.create(
            url="https://example.com/unattended-only",
            status="hold",
            attended=False,
        )
        resp = self._claim(attended=True)
        self.assertEqual(resp.status_code, 204)
