"""Single FIFO hold queue — claim-next is no longer partitioned.

A scrape is a scrape: headed-vs-headless is a property of the *runner*, not
a flag stamped on the row. POST /api/v1/scrapes/claim-next/ draws from one
FIFO queue over ``status='hold', claimed_at IS NULL`` ordered by
``created_at`` (NULLs first as the oldest), with ``id`` as a stable
tiebreak. The de-partitioned claim must:

- claim the OLDEST hold first, regardless of any (now-removed/ignored)
  ``attended`` value in the request body, and
- never error when a legacy runner still sends ``attended`` — the field is
  silently ignored, not partitioned on.
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


def _make_hold(url, *, age_minutes):
    """Create a hold with a deterministic created_at (auto_now_add, so
    override it via a post-insert .update()) for a stable FIFO order."""
    scrape = Scrape.objects.create(url=url, status="hold")
    created_at = timezone.now() - timedelta(minutes=age_minutes)
    Scrape.objects.filter(pk=scrape.pk).update(created_at=created_at)
    scrape.refresh_from_db()
    return scrape


class TestClaimNextSingleFifo(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="runner", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _claim(self, **body):
        return self.client.post(CLAIM_URL, data=body, format="json")

    def test_claims_oldest_hold_then_next_then_empty(self):
        """Two holds → claim-next returns the oldest, then the next, then
        204. One FIFO, no partition."""
        older = _make_hold("https://example.com/older", age_minutes=60)
        newer = _make_hold("https://example.com/newer", age_minutes=10)

        first = self._claim()
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(str(first.json()["data"]["id"]), str(older.id))

        second = self._claim()
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(str(second.json()["data"]["id"]), str(newer.id))

        third = self._claim()
        self.assertEqual(third.status_code, 204)

    def test_attended_in_body_is_ignored_not_partitioned(self):
        """A legacy runner sending ``attended: true`` still claims the single
        oldest hold — the flag is ignored, not used to partition the queue,
        and the request does not error."""
        oldest = _make_hold("https://example.com/oldest", age_minutes=90)
        _make_hold("https://example.com/mid", age_minutes=30)

        resp = self._claim(attended=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(str(resp.json()["data"]["id"]), str(oldest.id))

    def test_empty_queue_returns_204(self):
        resp = self._claim()
        self.assertEqual(resp.status_code, 204)
