"""PACA CC-74: observability fallback for unclaimed scrape holds.

When no scrape runner polls a claim partition, its status='hold',
claimed_at IS NULL rows never get claimed and rot invisibly ("I didn't
see the poller grab it"). ``sweep_stale_unclaimed_holds`` is the
read-only fallback: a per-partition WARNING so a dead/absent runner
surfaces, and ``scrape_hold_queue_health`` (also served by
GET /api/v1/admin/scrape-queue-health/) is the count surface for a future
admin badge.

The age signal is the audit table — Scrape has no created_at and an
unclaimed hold has claimed_at IS NULL — so each helper backdates a
``ScrapeStatus`` row (its auto_now_add ``created_at`` overridden via a
post-insert .update()).
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.test import APIClient

from job_hunting.lib.tasks import (
    scrape_hold_queue_health,
    sweep_stale_unclaimed_holds,
)
from job_hunting.models import Scrape, ScrapeStatus, Status


User = get_user_model()


def _make_status_row(scrape, status_label, created_at):
    """Append a backdated ScrapeStatus row (created_at is auto_now_add, so
    override it with a post-insert .update())."""
    status_obj, _ = Status.objects.get_or_create(
        status=status_label, defaults={"status_type": "scrape"}
    )
    ss = ScrapeStatus.objects.create(
        scrape=scrape, status=status_obj, logged_at=created_at
    )
    ScrapeStatus.objects.filter(pk=ss.pk).update(created_at=created_at)
    return ss


def _make_hold(
    *,
    age_minutes,
    attended=False,
    status="hold",
    claimed_at=None,
    url=None,
    with_status_row=True,
):
    scrape = Scrape.objects.create(
        url=url or "https://example.com/hold",
        status=status,
        attended=attended,
        claimed_at=claimed_at,
    )
    if with_status_row:
        held_at = timezone.now() - timedelta(minutes=age_minutes)
        _make_status_row(scrape, "hold", held_at)
    return scrape


class TestSweepStaleUnclaimedHolds(TestCase):
    def test_stale_unattended_hold_warns_and_counts(self):
        """A > threshold unattended unclaimed hold → counted stale + one
        scrape.holds.stale WARNING partitioned attended=False."""
        _make_hold(age_minutes=85, attended=False)

        with mock.patch("job_hunting.lib.tasks.logger") as log:
            result = sweep_stale_unclaimed_holds(threshold_minutes=30)

        self.assertEqual(result["hold_unclaimed_total"], 1)
        self.assertEqual(result["hold_unclaimed_stale"], 1)
        self.assertGreaterEqual(result["oldest_hold_age_seconds"], 85 * 60)
        bd = result["attended_breakdown"]
        self.assertEqual(bd["false"]["stale"], 1)
        self.assertEqual(bd["true"]["stale"], 0)

        log.warning.assert_called_once()
        args = log.warning.call_args.args
        self.assertEqual(
            args[0],
            "scrape.holds.stale count=%s oldest_age_min=%s attended=%s",
        )
        self.assertEqual(args[1], 1)  # count
        self.assertGreaterEqual(args[2], 85)  # oldest_age_min
        self.assertIs(args[3], False)  # attended flag

    def test_fresh_hold_not_stale_no_warning(self):
        """A hold younger than the threshold counts in total but not stale,
        and emits no warning."""
        _make_hold(age_minutes=5, attended=False)

        with mock.patch("job_hunting.lib.tasks.logger") as log:
            result = sweep_stale_unclaimed_holds(threshold_minutes=30)

        self.assertEqual(result["hold_unclaimed_total"], 1)
        self.assertEqual(result["hold_unclaimed_stale"], 0)
        log.warning.assert_not_called()

    def test_claimed_running_row_excluded(self):
        """Claimed/running and completed rows are not unclaimed holds →
        ignored entirely."""
        _make_hold(
            age_minutes=120,
            attended=False,
            status="running",
            claimed_at=timezone.now(),
        )
        Scrape.objects.create(
            url="https://example.com/done", status="completed"
        )

        result = sweep_stale_unclaimed_holds(threshold_minutes=30)

        self.assertEqual(result["hold_unclaimed_total"], 0)
        self.assertEqual(result["hold_unclaimed_stale"], 0)
        self.assertIsNone(result["oldest_hold_age_seconds"])

    def test_both_partitions_break_down_and_warn_separately(self):
        """Stale holds in both partitions → both breakdown buckets count and
        a warning fires per partition, each carrying its attended flag."""
        _make_hold(age_minutes=90, attended=False, url="https://e.com/u")
        _make_hold(age_minutes=40, attended=True, url="https://e.com/a")

        with mock.patch("job_hunting.lib.tasks.logger") as log:
            result = sweep_stale_unclaimed_holds(threshold_minutes=30)

        bd = result["attended_breakdown"]
        self.assertEqual(bd["false"]["stale"], 1)
        self.assertEqual(bd["true"]["stale"], 1)
        self.assertEqual(result["hold_unclaimed_stale"], 2)

        self.assertEqual(log.warning.call_count, 2)
        flags = {c.args[3] for c in log.warning.call_args_list}
        self.assertEqual(flags, {False, True})

    def test_hold_without_status_row_counted_but_not_stale(self):
        """A hold with no ScrapeStatus has held_at=NULL — counted in total
        but never stale (can't prove age)."""
        _make_hold(age_minutes=0, attended=False, with_status_row=False)

        result = sweep_stale_unclaimed_holds(threshold_minutes=30)

        self.assertEqual(result["hold_unclaimed_total"], 1)
        self.assertEqual(result["hold_unclaimed_stale"], 0)
        self.assertIsNone(result["oldest_hold_age_seconds"])

    def test_threshold_override(self):
        """A 90m hold is stale at threshold=30 but fresh at threshold=120."""
        _make_hold(age_minutes=90, attended=False)

        self.assertEqual(
            scrape_hold_queue_health(stale_minutes=30)["hold_unclaimed_stale"],
            1,
        )
        self.assertEqual(
            scrape_hold_queue_health(stale_minutes=120)["hold_unclaimed_stale"],
            0,
        )

    def test_redo_clock_uses_latest_hold_not_original(self):
        """A re-held scrape is dated from its most-recent hold ScrapeStatus,
        not the original: hold(120m) → running(90m) → failed(80m) →
        re-hold(5m) is fresh at threshold=30 (latest hold is 5m old)."""
        scrape = Scrape.objects.create(
            url="https://example.com/redo", status="hold", attended=False
        )
        now = timezone.now()
        _make_status_row(scrape, "hold", now - timedelta(minutes=120))
        _make_status_row(scrape, "running", now - timedelta(minutes=90))
        _make_status_row(scrape, "failed", now - timedelta(minutes=80))
        _make_status_row(scrape, "hold", now - timedelta(minutes=5))

        result = scrape_hold_queue_health(stale_minutes=30)
        self.assertEqual(result["hold_unclaimed_total"], 1)
        self.assertEqual(result["hold_unclaimed_stale"], 0)


class TestScrapeQueueHealthEndpoint(TestCase):
    URL = "/api/v1/admin/scrape-queue-health/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.staff = User.objects.create_user(
            username="root", password="pw", is_staff=True
        )

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, http_status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_rejected(self):
        resp = self.client.get(self.URL)
        self.assertIn(
            resp.status_code,
            (
                http_status.HTTP_401_UNAUTHORIZED,
                http_status.HTTP_403_FORBIDDEN,
            ),
        )

    def test_staff_gets_counts(self):
        _make_hold(age_minutes=85, attended=False)
        _make_hold(age_minutes=5, attended=False, url="https://e.com/fresh")
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertEqual(data["hold_unclaimed_total"], 2)
        self.assertEqual(data["hold_unclaimed_stale"], 1)
        self.assertGreaterEqual(data["oldest_hold_age_seconds"], 85 * 60)
        self.assertEqual(data["attended_breakdown"]["false"]["stale"], 1)
        self.assertEqual(data["attended_breakdown"]["true"]["total"], 0)

    def test_stale_minutes_query_override(self):
        _make_hold(age_minutes=90, attended=False)
        self.client.force_authenticate(user=self.staff)

        resp = self.client.get(self.URL, {"stale_minutes": 120})
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertEqual(data["stale_minutes"], 120)
        self.assertEqual(data["hold_unclaimed_stale"], 0)


class TestStaleUnclaimedHoldScheduleRegistered(TestCase):
    """Migration 0113 registers the sweep as a django-q2 Schedule. Pin the
    cadence so a stray reset or migration rollback doesn't silently disable
    the staleness observability."""

    def test_schedule_row_exists(self):
        from django_q.models import Schedule

        row = Schedule.objects.filter(
            name="sweep_stale_unclaimed_holds"
        ).first()
        self.assertIsNotNone(
            row, "0113 migration should register the schedule"
        )
        self.assertEqual(
            row.func, "job_hunting.lib.tasks.sweep_stale_unclaimed_holds"
        )
        self.assertEqual(row.schedule_type, Schedule.MINUTES)
        self.assertEqual(row.minutes, 5)
        self.assertEqual(row.repeats, -1)
