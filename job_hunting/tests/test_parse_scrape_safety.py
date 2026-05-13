"""Safety-net tests for parse_scrape and the sweep_stuck_extracting command.

The bug this guards against: scrape 273 was found stuck in
``status='extracting'`` indefinitely. The daemon-thread that runs
``parse_scrape._run`` died (container restart, OOM, raise inside
``_update_scrape_profile`` etc.) between the ``extracting`` flip and
the success/failure terminal flip, so the frontend kept polling a row
that would never resolve.

These tests pin two layers of defense:

1. ``parse_scrape._run`` wraps its body in try/finally + a
   ``reached_terminal`` flag — even an unhandled exception leaves the
   row in a terminal state.
2. The ``sweep_stuck_extracting`` management command catches the rare
   case where finally itself can't run (SIGKILL, hardware reboot).
"""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from job_hunting.lib.parsers.job_post_extractor import parse_scrape
from job_hunting.models import Scrape
from job_hunting.models.scrape_status import ScrapeStatus


User = get_user_model()


class TestParseScrapeSafetyNet(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="safety", password="pw")

    def _make_scrape(self):
        return Scrape.objects.create(
            url="https://example.com/x",
            job_content="x" * 200,
            status="pending",
            created_by=self.user,
            source="extension",
        )

    def test_unhandled_exception_flips_stuck_extracting_to_failed(self):
        # Patch ``JobPostExtractor`` itself so its instantiation raises
        # — that's BEFORE the existing try/except around parser.parse,
        # so the exception escapes the inner guards and hits the
        # outer safety-net try/finally. Without the safety net, the
        # scrape would stay in ``extracting`` forever.
        scrape = self._make_scrape()
        with patch(
            "job_hunting.lib.parsers.job_post_extractor.JobPostExtractor"
        ) as klass:
            klass.side_effect = RuntimeError("simulated daemon death")
            parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "failed")
        latest = scrape.scrape_statuses.order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertIn("died before terminal", latest.note)

    def test_safety_net_does_not_crash_when_log_status_raises(self):
        # If both the work AND the safety-net's flip fail, parse_scrape
        # must still return cleanly (no propagated exception) — a stuck
        # row is bad, a crashed background thread is worse because it
        # can take adjacent work down with it.
        scrape = self._make_scrape()
        with patch(
            "job_hunting.lib.parsers.job_post_extractor.JobPostExtractor"
        ) as klass:
            klass.side_effect = RuntimeError("first failure")
            with patch(
                "job_hunting.lib.scraper._log_scrape_status",
                side_effect=RuntimeError("logging is broken too"),
            ):
                # Should not raise, even though both paths fail.
                parse_scrape(scrape.id, user_id=self.user.id, sync=True)

    def test_pre_extracting_returns_do_not_trigger_safety_net(self):
        # Early-bail returns (scrape not found / already linked / no
        # content) happen BEFORE the ``extracting`` status flip, so
        # there's no stuck state to clean up. The safety net must not
        # spuriously flip these to failed.
        parse_scrape(999_999, user_id=self.user.id, sync=True)
        # No scrape exists; nothing to assert except the call returned.
        self.assertFalse(
            ScrapeStatus.objects.filter(scrape_id=999_999).exists(),
            "Safety net should not log against a nonexistent scrape",
        )


class TestSweepStuckExtractingCommand(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sweep", password="pw")

    def _create_stuck_scrape(self, *, status: str, age_minutes: int):
        """Create a scrape in ``status`` with a ScrapeStatus row dated
        ``age_minutes`` ago. The sweep command keys off the latest
        ``ScrapeStatus.logged_at`` to decide what's stuck."""
        s = Scrape.objects.create(
            url=f"https://example.com/{status}-{age_minutes}",
            status=status,
            created_by=self.user,
            source="extension",
        )
        # Log the status so the sweep query has a logged_at to read,
        # then backdate it with a raw UPDATE (auto_now would otherwise
        # always reset it to now()).
        from job_hunting.lib.scraper import _log_scrape_status
        _log_scrape_status(s.id, status)
        ScrapeStatus.objects.filter(scrape_id=s.id).update(
            logged_at=timezone.now() - timedelta(minutes=age_minutes)
        )
        return s

    def test_sweeps_old_extracting_rows(self):
        old = self._create_stuck_scrape(status="extracting", age_minutes=20)
        recent = self._create_stuck_scrape(status="extracting", age_minutes=5)
        out = StringIO()
        call_command("sweep_stuck_extracting", "--min-age-minutes=15", stdout=out)
        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(old.status, "failed")
        self.assertEqual(recent.status, "extracting")
        self.assertIn("flipped 1 stuck scrapes", out.getvalue())
        # Sweep note should be on the latest ScrapeStatus row
        latest = old.scrape_statuses.order_by("-id").first()
        self.assertIn("swept", latest.note)

    def test_sweeps_updating_profile_state_too(self):
        old = self._create_stuck_scrape(status="updating_profile", age_minutes=20)
        call_command("sweep_stuck_extracting", "--min-age-minutes=15")
        old.refresh_from_db()
        self.assertEqual(old.status, "failed")

    def test_dry_run_reports_without_flipping(self):
        old = self._create_stuck_scrape(status="extracting", age_minutes=20)
        out = StringIO()
        call_command(
            "sweep_stuck_extracting",
            "--min-age-minutes=15",
            "--dry-run",
            stdout=out,
        )
        old.refresh_from_db()
        self.assertEqual(old.status, "extracting")
        self.assertIn("would flip 0 stuck scrapes", out.getvalue())
