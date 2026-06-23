"""PACA CC #32: staleness fallback for orphaned attended holds.

Attended-scrape routing (CC #31) partitions the status='hold' claim queue
on Scrape.attended; an attended=True hold orphans in `hold` forever if no
attended runner polls. ``sweep_orphaned_attended_holds`` is the fallback:
always-on observability plus an opt-in (TTL > 0) auto-demote leg.

The age signal is the audit table — Scrape has no created_at and an
unclaimed hold has claimed_at IS NULL — so each helper backdates a
``ScrapeStatus`` row (its auto_now_add ``created_at`` is overridden via a
post-insert .update()).
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from job_hunting.lib.tasks import sweep_orphaned_attended_holds
from job_hunting.models import JobPost, Scrape, ScrapeStatus, Status


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


class TestSweepOrphanedAttendedHolds(TestCase):
    def _make_attended_hold(
        self, *, age_minutes, attended=True, status="hold", url=None
    ):
        scrape = Scrape.objects.create(
            url=url or "https://example.com/attended",
            status=status,
            attended=attended,
            claimed_at=None,
        )
        held_at = timezone.now() - timedelta(minutes=age_minutes)
        _make_status_row(scrape, "hold", held_at)
        return scrape

    def test_fail_demotes_aged_attended_hold(self):
        """action='fail' → status failed + failure_reason + a failed
        ScrapeStatus audit row + events.notify once; the linked JobPost
        stub stays complete=False so a later attended redo recovers it."""
        scrape = self._make_attended_hold(age_minutes=120)
        jp = JobPost.objects.create(
            title="Orphaned role",
            link="https://example.com/jobs/orphan",
            complete=False,
        )
        scrape.job_post = jp
        scrape.save(update_fields=["job_post"])

        with mock.patch("job_hunting.lib.events.notify") as notify:
            result = sweep_orphaned_attended_holds(
                ttl_minutes=30, action="fail", warn_minutes=30
            )

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "failed")
        self.assertIn("attended hold expired", scrape.failure_reason)
        self.assertIn('--attended', scrape.failure_reason)
        self.assertTrue(
            scrape.scrape_statuses.filter(status__status="failed").exists()
        )
        notify.assert_called_once_with(
            "scrape", scrape.id, "failed", scrape.created_by_id
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["skipped"], 0)

        jp.refresh_from_db()
        self.assertFalse(jp.complete)

    def test_unattended_demotes_then_default_claim_returns_it(self):
        """action='unattended' → attended=False, status still hold,
        claimed_at still NULL; the default (attended=False) claim partition
        — exactly what claim_next filters on — now returns the row."""
        scrape = self._make_attended_hold(age_minutes=120)

        result = sweep_orphaned_attended_holds(
            ttl_minutes=30, action="unattended"
        )

        scrape.refresh_from_db()
        self.assertFalse(scrape.attended)
        self.assertEqual(scrape.status, "hold")
        self.assertIsNone(scrape.claimed_at)
        self.assertEqual(result["demoted"], 1)
        self.assertEqual(result["failed"], 0)

        claimable = (
            Scrape.objects.filter(
                status="hold", claimed_at__isnull=True, attended=False
            )
            .order_by("id")
            .first()
        )
        self.assertIsNotNone(claimable)
        self.assertEqual(claimable.id, scrape.id)

    def test_not_before_ttl_untouched(self):
        """A hold younger than the TTL is not a candidate."""
        scrape = self._make_attended_hold(age_minutes=10)

        result = sweep_orphaned_attended_holds(ttl_minutes=30, action="fail")

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertTrue(scrape.attended)
        self.assertEqual(result["failed"], 0)

    def test_non_attended_aged_hold_untouched(self):
        """An aged but attended=False hold is the normal unattended queue —
        never failed/demoted, and never counted as orphaned."""
        scrape = self._make_attended_hold(age_minutes=120, attended=False)

        result = sweep_orphaned_attended_holds(
            ttl_minutes=30, action="fail", warn_minutes=30
        )

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertFalse(scrape.attended)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["stale_count"], 0)

    @override_settings(
        CC_ATTENDED_HOLD_TTL_MINUTES=0,
        CC_ATTENDED_HOLD_TTL_ACTION="fail",
        CC_ATTENDED_HOLD_WARN_MINUTES=30,
    )
    def test_flag_off_no_mutation_only_count(self):
        """Default settings (TTL=0) → auto-demote OFF: no mutation, only the
        observability count is returned (and no TTL cutoff)."""
        scrape = self._make_attended_hold(age_minutes=120)

        # No args → reads settings → TTL 0 → observability only.
        result = sweep_orphaned_attended_holds()

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertTrue(scrape.attended)
        self.assertEqual(result["ttl_minutes"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["demoted"], 0)
        self.assertEqual(result["stale_count"], 1)
        self.assertNotIn("cutoff", result)

    def test_no_double_claim_race_fail(self):
        """A row claimed by a concurrent attended runner between the
        candidate snapshot and the guarded mutate: the fail-leg guard
        matches 0 rows → the row is left running, never failed."""
        scrape = self._make_attended_hold(age_minutes=120)
        # Simulate the attended claim-next winning the race.
        Scrape.objects.filter(pk=scrape.id).update(
            status="running",
            claimed_at=timezone.now(),
            claimed_by="attended-omarchy",
        )

        with mock.patch(
            "job_hunting.lib.tasks._orphaned_attended_hold_ids",
            return_value=[scrape.id],
        ), mock.patch("job_hunting.lib.events.notify") as notify:
            result = sweep_orphaned_attended_holds(
                ttl_minutes=30, action="fail"
            )

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")
        self.assertEqual(scrape.claimed_by, "attended-omarchy")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 1)
        notify.assert_not_called()

    def test_no_double_claim_race_unattended(self):
        """Same race, action='unattended': the guarded bulk demote excludes
        the now-running row → not demoted."""
        scrape = self._make_attended_hold(age_minutes=120)
        Scrape.objects.filter(pk=scrape.id).update(
            status="running",
            claimed_at=timezone.now(),
            claimed_by="attended-omarchy",
        )

        with mock.patch(
            "job_hunting.lib.tasks._orphaned_attended_hold_ids",
            return_value=[scrape.id],
        ):
            result = sweep_orphaned_attended_holds(
                ttl_minutes=30, action="unattended"
            )

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")
        self.assertTrue(scrape.attended)
        self.assertEqual(result["demoted"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_redo_clock_uses_latest_hold_not_original(self):
        """A re-held scrape is timed from its most-recent hold ScrapeStatus,
        not the original. hold(120m) → running(90m) → failed(80m) →
        re-hold(5m): a 30m TTL must NOT touch it (latest hold is 5m old);
        a 2m TTL must (the re-held timestamp is what's stale)."""
        scrape = Scrape.objects.create(
            url="https://example.com/redo",
            status="hold",
            attended=True,
            claimed_at=None,
        )
        now = timezone.now()
        _make_status_row(scrape, "hold", now - timedelta(minutes=120))
        _make_status_row(scrape, "running", now - timedelta(minutes=90))
        _make_status_row(scrape, "failed", now - timedelta(minutes=80))
        _make_status_row(scrape, "hold", now - timedelta(minutes=5))

        # 30m TTL: latest hold (5m) is younger than the cutoff → untouched.
        result30 = sweep_orphaned_attended_holds(
            ttl_minutes=30, action="fail", warn_minutes=30
        )
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertEqual(result30["failed"], 0)
        self.assertEqual(result30["stale_count"], 0)

        # 2m TTL: the re-held timestamp (5m) is now stale → failed.
        result2 = sweep_orphaned_attended_holds(ttl_minutes=2, action="fail")
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "failed")
        self.assertEqual(result2["failed"], 1)

    def test_observability_leg_returns_stale_count(self):
        """The read-only leg counts attended holds older than WARN minutes,
        ignoring younger ones — independent of the auto-demote TTL."""
        self._make_attended_hold(
            age_minutes=45, url="https://example.com/old"
        )
        self._make_attended_hold(
            age_minutes=10, url="https://example.com/young"
        )

        result = sweep_orphaned_attended_holds(ttl_minutes=0, warn_minutes=30)

        self.assertEqual(result["stale_count"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["demoted"], 0)


class TestAttendedHoldSweepScheduleRegistered(TestCase):
    """Migration 0111 registers the sweep as a django-q2 Schedule. Pin the
    cadence so a stray reset or migration rollback doesn't silently disable
    the staleness fallback."""

    def test_schedule_row_exists(self):
        from django_q.models import Schedule

        row = Schedule.objects.filter(
            name="sweep_orphaned_attended_holds"
        ).first()
        self.assertIsNotNone(row, "0111 migration should register the schedule")
        self.assertEqual(
            row.func, "job_hunting.lib.tasks.sweep_orphaned_attended_holds"
        )
        self.assertEqual(row.schedule_type, Schedule.MINUTES)
        self.assertEqual(row.minutes, 5)
        self.assertEqual(row.repeats, -1)
