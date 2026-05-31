"""Phase 2 of Plans/Scrape runner: lease-timeout sweep.

Tests the recovery path for crashed runners. A runner that picks up a
scrape via /scrapes/claim-next/ and then dies leaves the row stuck at
status='running' with a stale claimed_at. The sweep task identifies
those rows (claimed_at < cutoff AND status is non-terminal) and resets
them back to status='hold' so the next claim picks them up.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from job_hunting.lib.tasks import sweep_stale_scrape_claims
from job_hunting.models import Scrape


class TestSweepStaleScrapeClaims(TestCase):
    def test_resets_stale_running_claim(self):
        stale_at = timezone.now() - timedelta(minutes=30)
        scrape = Scrape.objects.create(
            url="https://example.com/stale",
            status="running",
            claimed_at=stale_at,
            claimed_by="omarchy",
        )

        result = sweep_stale_scrape_claims(threshold_minutes=15)

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertIsNone(scrape.claimed_at)
        self.assertIsNone(scrape.claimed_by)
        self.assertEqual(result["reset"], 1)
        self.assertEqual(result["threshold_minutes"], 15)

    def test_resets_stale_extracting_claim(self):
        """`extracting` is non-terminal — a runner that died during the
        LLM call should also be recoverable."""
        stale_at = timezone.now() - timedelta(minutes=30)
        scrape = Scrape.objects.create(
            url="https://example.com/extracting",
            status="extracting",
            claimed_at=stale_at,
            claimed_by="pibu",
        )

        result = sweep_stale_scrape_claims(threshold_minutes=15)

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertEqual(result["reset"], 1)

    def test_skips_fresh_claim(self):
        """A running scrape whose claim was just heartbeat'd is healthy.
        Don't reap it."""
        fresh_at = timezone.now() - timedelta(minutes=2)
        scrape = Scrape.objects.create(
            url="https://example.com/fresh",
            status="running",
            claimed_at=fresh_at,
            claimed_by="omarchy",
        )

        result = sweep_stale_scrape_claims(threshold_minutes=15)

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")
        self.assertEqual(scrape.claimed_by, "omarchy")
        self.assertEqual(result["reset"], 0)

    def test_skips_terminal_claim(self):
        """A completed scrape with a stale claimed_at (shouldn't happen
        post-Phase 1, but defense-in-depth) should NOT be reset — terminal
        rows aren't candidates for re-running."""
        stale_at = timezone.now() - timedelta(minutes=30)
        Scrape.objects.create(
            url="https://example.com/completed",
            status="completed",
            claimed_at=stale_at,  # stale but irrelevant
            claimed_by="omarchy",
        )

        result = sweep_stale_scrape_claims(threshold_minutes=15)

        self.assertEqual(result["reset"], 0)

    def test_skips_hold_rows(self):
        """A scrape already at status='hold' with no claim is healthy —
        the claim queue is the source of truth, not the sweep."""
        Scrape.objects.create(
            url="https://example.com/waiting",
            status="hold",
            claimed_at=None,
            claimed_by=None,
        )

        result = sweep_stale_scrape_claims(threshold_minutes=15)

        self.assertEqual(result["reset"], 0)

    def test_returns_zero_when_no_stale(self):
        """No stale rows → reset=0, no exception."""
        result = sweep_stale_scrape_claims(threshold_minutes=15)
        self.assertEqual(result["reset"], 0)
        self.assertIn("cutoff", result)

    def test_threshold_parameter_respected(self):
        """A 5-minute-stale claim is healthy under threshold=15 but
        stale under threshold=2. Pins the parameter contract."""
        five_min_ago = timezone.now() - timedelta(minutes=5)
        scrape = Scrape.objects.create(
            url="https://example.com/marginal",
            status="running",
            claimed_at=five_min_ago,
            claimed_by="omarchy",
        )

        # Threshold 15 → not stale.
        result_15 = sweep_stale_scrape_claims(threshold_minutes=15)
        self.assertEqual(result_15["reset"], 0)
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")

        # Threshold 2 → stale.
        result_2 = sweep_stale_scrape_claims(threshold_minutes=2)
        self.assertEqual(result_2["reset"], 1)
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")

    def test_bulk_reset(self):
        """N stale rows reset in one pass, FIFO unchanged. The sweep is
        idempotent — running it again immediately returns reset=0."""
        stale_at = timezone.now() - timedelta(minutes=30)
        ids = []
        for i in range(4):
            s = Scrape.objects.create(
                url=f"https://example.com/bulk-{i}",
                status="running",
                claimed_at=stale_at,
                claimed_by="dead-runner",
            )
            ids.append(s.id)

        first = sweep_stale_scrape_claims(threshold_minutes=15)
        self.assertEqual(first["reset"], 4)

        # Second pass: no new stale, reset=0.
        second = sweep_stale_scrape_claims(threshold_minutes=15)
        self.assertEqual(second["reset"], 0)

        # All rows back to hold.
        for sid in ids:
            self.assertEqual(Scrape.objects.get(pk=sid).status, "hold")


class TestSweepScheduleRegistered(TestCase):
    """Migration 0086 should have registered the sweep as a django-q2
    Schedule with the expected cadence. Pin so a stray reset or migration
    rollback doesn't silently disable lease recovery in production."""

    def test_schedule_row_exists(self):
        from django_q.models import Schedule

        row = Schedule.objects.filter(name="sweep_stale_scrape_claims").first()
        self.assertIsNotNone(row, "0086 migration should register the schedule")
        self.assertEqual(
            row.func, "job_hunting.lib.tasks.sweep_stale_scrape_claims"
        )
        self.assertEqual(row.schedule_type, Schedule.MINUTES)
        self.assertEqual(row.minutes, 5)
        self.assertEqual(row.repeats, -1)
