"""CC-96 — claim-next FIFO correctness on NanoID PKs + anti-wedge.

Pins the durable fix for the prod claim-next wedge (post CC-114, where
the attended partition was dropped — the hold queue is now a single FIFO):

* FIFO ordering follows ``created_at`` (the CC-77 #86 / migration 0125
  key), NOT the random NanoID ``id`` — so a queue clears in arrival
  order even though ids no longer increase monotonically.
* A claim can never pin a worker for the gunicorn request timeout: the
  transaction is bounded by ``lock_timeout`` / ``statement_timeout`` and
  a contention/timeout error is surfaced as a fast 204. A concurrently
  locked hold is skipped (SELECT FOR UPDATE SKIP LOCKED), not waited on.

See also test_scrape_claim_next.py (base claim contract),
test_scrape_claim_next_fifo.py (single-queue FIFO contract),
test_stale_unclaimed_hold_sweep.py (CC-32 stale-hold staleness sweep), and
test_scrape_claim_sweep.py (CC-32 crashed-claim lease sweep).
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import (
    OperationalError,
    connection,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from job_hunting.models import Scrape

User = get_user_model()

CLAIM_URL = "/api/v1/scrapes/claim-next/"


def _set_created_at(scrape, when):
    """created_at is auto_now_add (ignored on insert) — stamp it via an
    UPDATE so a test can control FIFO arrival order independent of the
    insert order / random NanoID id."""
    Scrape.objects.filter(id=scrape.id).update(created_at=when)


class TestClaimFifoOrdering(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="runner", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self):
        return self.client.post(
            CLAIM_URL,
            data={"runner_name": "omarchy"},
            format="json",
        )

    def test_fifo_follows_created_at_not_nanoid_id(self):
        """Claim order tracks created_at, not the (random) NanoID id.

        Build a queue whose created_at order is deliberately decoupled
        from insert order, and assert it clears oldest-first regardless
        of how the NanoID ids happen to sort.
        """
        a = Scrape.objects.create(url="https://example.com/a", status="hold")
        b = Scrape.objects.create(url="https://example.com/b", status="hold")
        c = Scrape.objects.create(url="https://example.com/c", status="hold")

        now = timezone.now()
        # Arrival order: c (oldest) -> a -> b (newest).
        _set_created_at(c, now - timedelta(minutes=3))
        _set_created_at(a, now - timedelta(minutes=2))
        _set_created_at(b, now - timedelta(minutes=1))

        claimed = []
        for _ in range(3):
            resp = self._post()
            self.assertEqual(resp.status_code, 200, resp.content)
            claimed.append(str(resp.json()["data"]["id"]))

        self.assertEqual(claimed, [str(c.id), str(a.id), str(b.id)])
        self.assertEqual(self._post().status_code, 204)

    def test_null_created_at_sorts_first(self):
        """A pre-CC-77 row (NULL created_at) is the oldest hold and is
        claimed before a row that carries a created_at (nulls_first)."""
        legacy = Scrape.objects.create(
            url="https://example.com/legacy", status="hold"
        )
        _set_created_at(legacy, None)
        recent = Scrape.objects.create(
            url="https://example.com/recent", status="hold"
        )
        _set_created_at(recent, timezone.now())

        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(str(resp.json()["data"]["id"]), str(legacy.id))

    def test_db_contention_returns_fast_204(self):
        """A lock_timeout / statement_timeout (or transient OperationalError)
        on the claim is surfaced as 204, not a 5xx or a hang — the runner
        just retries. The hold is left untouched and still claimable."""
        Scrape.objects.create(
            url="https://example.com/contended", status="hold"
        )
        with mock.patch.object(
            Scrape.objects,
            "select_for_update",
            side_effect=OperationalError(
                "canceling statement due to statement timeout"
            ),
        ):
            resp = self._post()

        self.assertEqual(resp.status_code, 204, resp.content)
        # Untouched: still a claimable hold once contention clears.
        self.assertEqual(
            Scrape.objects.filter(status="hold", claimed_at__isnull=True).count(),
            1,
        )


class TestClaimWedgeConcurrency(TransactionTestCase):
    """True multi-connection concurrency — needs TransactionTestCase so
    rows committed by one connection are visible to another (and so
    SELECT FOR UPDATE actually takes DB-level row locks)."""

    def setUp(self):
        self.user = User.objects.create_user(username="runner", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self):
        return self.client.post(
            CLAIM_URL,
            data={"runner_name": "omarchy"},
            format="json",
        )

    def _hold_row_lock(self, scrape_id, acquired, release, errors):
        """Lock ``scrape_id`` FOR UPDATE in a dedicated connection and hold
        it until ``release`` is set — mimics a stuck/abandoned claim txn."""
        try:
            with transaction.atomic():
                list(
                    Scrape.objects.select_for_update().filter(id=scrape_id)
                )
                acquired.set()
                release.wait(timeout=30)
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)
            acquired.set()
        finally:
            connection.close()

    def test_locked_only_hold_does_not_wedge(self):
        """The only hold is locked by a stuck txn. SKIP LOCKED skips it →
        claim returns a FAST 204 (not a 120s wedge)."""
        scrape = Scrape.objects.create(
            url="https://example.com/locked", status="hold"
        )
        acquired, release, errors = (
            threading.Event(),
            threading.Event(),
            [],
        )
        worker = threading.Thread(
            target=self._hold_row_lock,
            args=(scrape.id, acquired, release, errors),
        )
        worker.start()
        try:
            self.assertTrue(
                acquired.wait(timeout=10), "lock thread never acquired"
            )
            self.assertFalse(errors, errors)
            start = time.monotonic()
            resp = self._post()
            elapsed = time.monotonic() - start
        finally:
            release.set()
            worker.join(timeout=10)

        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertLess(
            elapsed, 15, f"claim took {elapsed:.1f}s — wedge not prevented"
        )

    def test_locked_oldest_hold_claims_next(self):
        """Oldest hold is locked; the claim skips it and returns the
        next-oldest unlocked one (SKIP LOCKED + FIFO)."""
        now = timezone.now()
        oldest = Scrape.objects.create(
            url="https://example.com/oldest", status="hold"
        )
        nxt = Scrape.objects.create(
            url="https://example.com/next", status="hold"
        )
        _set_created_at(oldest, now - timedelta(minutes=5))
        _set_created_at(nxt, now - timedelta(minutes=1))

        acquired, release, errors = (
            threading.Event(),
            threading.Event(),
            [],
        )
        worker = threading.Thread(
            target=self._hold_row_lock,
            args=(oldest.id, acquired, release, errors),
        )
        worker.start()
        try:
            self.assertTrue(
                acquired.wait(timeout=10), "lock thread never acquired"
            )
            self.assertFalse(errors, errors)
            start = time.monotonic()
            resp = self._post()
            elapsed = time.monotonic() - start
        finally:
            release.set()
            worker.join(timeout=10)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(str(resp.json()["data"]["id"]), str(nxt.id))
        self.assertLess(
            elapsed, 15, f"claim took {elapsed:.1f}s — wedge not prevented"
        )

    def tearDown(self):
        # Defensive: ensure no leaked per-thread connections linger.
        connections.close_all()
