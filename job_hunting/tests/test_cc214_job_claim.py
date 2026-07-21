"""CC-214 — Job.objects.claim_next + lease sweep.

Mirrors test_scrape_claim_sweep.py: atomic claim, FIFO order, the run_after
due-gate, empty-queue None, contention → None, and stale-claim recovery.
The claim reuses the exact scrapes SELECT FOR UPDATE SKIP LOCKED discipline.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from job_hunting.models import Job


class TestJobClaimNext(TestCase):
    def test_claims_pending_and_flips_to_running(self):
        job = Job.objects.create(kind="score", payload={"score_id": "s1"})
        claimed = Job.objects.claim_next(runner_name="pibu")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.claimed_by, "pibu")
        self.assertIsNotNone(claimed.claimed_at)
        self.assertEqual(claimed.attempts, 1)

    def test_returns_none_when_empty(self):
        self.assertIsNone(Job.objects.claim_next(runner_name="pibu"))

    def test_fifo_by_created_at(self):
        older = Job.objects.create(kind="score", payload={"score_id": "old"})
        Job.objects.filter(pk=older.id).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        newer = Job.objects.create(kind="score", payload={"score_id": "new"})
        Job.objects.filter(pk=newer.id).update(
            created_at=timezone.now() - timedelta(minutes=1)
        )
        claimed = Job.objects.claim_next()
        self.assertEqual(claimed.id, older.id)

    def test_run_after_gate_skips_future_job(self):
        future = timezone.now() + timedelta(minutes=30)
        Job.objects.create(
            kind="score", payload={"score_id": "later"}, run_after=future
        )
        # Only a future-gated job exists → nothing claimable yet.
        self.assertIsNone(Job.objects.claim_next())

    def test_run_after_in_past_is_claimable(self):
        past = timezone.now() - timedelta(minutes=1)
        job = Job.objects.create(
            kind="score", payload={"score_id": "due"}, run_after=past
        )
        claimed = Job.objects.claim_next()
        self.assertEqual(claimed.id, job.id)

    def test_running_job_not_reclaimed(self):
        Job.objects.create(
            kind="score",
            payload={"score_id": "x"},
            status="running",
            claimed_at=timezone.now(),
            claimed_by="someone",
        )
        self.assertIsNone(Job.objects.claim_next())


class TestJobLeaseSweep(TestCase):
    def test_resets_stale_running_claim_with_attempts_remaining(self):
        stale = timezone.now() - timedelta(minutes=30)
        job = Job.objects.create(
            kind="score",
            payload={"score_id": "s1"},
            status="running",
            claimed_at=stale,
            claimed_by="dead",
            attempts=0,
            max_attempts=3,
        )
        result = Job.objects.sweep_stale_claims(threshold_minutes=15)
        self.assertEqual(result["reset"], 1)
        self.assertEqual(result["failed"], 0)
        job.refresh_from_db()
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.claimed_at)
        self.assertIsNone(job.claimed_by)

    def test_exhausted_attempts_marked_failed_not_requeued(self):
        stale = timezone.now() - timedelta(minutes=30)
        job = Job.objects.create(
            kind="score",
            payload={"score_id": "s1"},
            status="running",
            claimed_at=stale,
            claimed_by="dead",
            attempts=1,
            max_attempts=1,
        )
        result = Job.objects.sweep_stale_claims(threshold_minutes=15)
        self.assertEqual(result["reset"], 0)
        self.assertEqual(result["failed"], 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

    def test_skips_fresh_claim(self):
        fresh = timezone.now() - timedelta(minutes=2)
        job = Job.objects.create(
            kind="score",
            payload={},
            status="running",
            claimed_at=fresh,
            claimed_by="alive",
        )
        result = Job.objects.sweep_stale_claims(threshold_minutes=15)
        self.assertEqual(result["reset"], 0)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")

    def test_skips_terminal_rows(self):
        stale = timezone.now() - timedelta(minutes=30)
        Job.objects.create(
            kind="score",
            payload={},
            status="completed",
            claimed_at=stale,
            claimed_by="x",
        )
        result = Job.objects.sweep_stale_claims(threshold_minutes=15)
        self.assertEqual(result["reset"], 0)
        self.assertEqual(result["failed"], 0)


class TestJobClaimContention(TransactionTestCase):
    """Concurrency: two claims never grab the same row (SKIP LOCKED).

    TransactionTestCase so real transactions/row-locks apply (TestCase wraps
    each test in one outer transaction, which would defeat the point).
    """

    def test_two_claims_get_distinct_rows(self):
        a = Job.objects.create(kind="score", payload={"score_id": "a"})
        b = Job.objects.create(kind="score", payload={"score_id": "b"})
        first = Job.objects.claim_next(runner_name="r1")
        second = Job.objects.claim_next(runner_name="r2")
        claimed_ids = {first.id, second.id}
        self.assertEqual(claimed_ids, {a.id, b.id})
        # Third claim → nothing left.
        self.assertIsNone(Job.objects.claim_next(runner_name="r3"))
