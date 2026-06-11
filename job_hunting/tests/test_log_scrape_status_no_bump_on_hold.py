"""Regression: _log_scrape_status must NOT bump claimed_at when transitioning
TO status='hold'.

`hold` is the pre-claim queue state — no runner owns the row yet, so
heartbeating claimed_at on hold would cause claim-next (filter:
claimed_at IS NULL) to silently skip the row forever. The "submit and
hold" UI path and the lease sweep both end on `hold`; both must leave
claimed_at NULL so the next claim-next picks it up.

This test pins the contract that broke prod 2026-06-11: 3 scrapes
(495, 501, 503) sat in hold with claimed_at set, claim-next returned
204 to a healthy runner for ~90 minutes.
"""
from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from job_hunting.lib.scraper import _log_scrape_status
from job_hunting.models import Scrape


class TestLogScrapeStatusNoBumpOnHold(TestCase):
    def test_hold_leaves_claimed_at_null(self):
        """Brand-new scrape created with status='hold' via the UI's
        submit-and-hold path. _log_scrape_status('hold') must NOT
        stamp claimed_at — otherwise claim-next will skip it forever."""
        scrape = Scrape.objects.create(
            url="https://example.com/new-hold",
            status="hold",
        )

        _log_scrape_status(scrape.id, "hold")

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "hold")
        self.assertIsNone(
            scrape.claimed_at,
            "hold is pre-claim; claimed_at must stay NULL so claim-next "
            "can pick it up.",
        )
        self.assertIsNone(scrape.claimed_by)

    def test_hold_clears_pre_existing_claim_via_terminal_or_sweep(self):
        """A scrape that was previously running (claimed_at set) and now
        comes back to 'hold' (e.g. via lease sweep) should also end
        with claimed_at NULL — but the sweep is the canonical resetter,
        not _log_scrape_status. We verify _log_scrape_status doesn't
        re-stamp claimed_at when called with 'hold' on a row that
        already has a non-null value: it should be left alone (the
        sweep cleared it; we don't undo that). In fact the simpler
        contract: the 'hold' branch never writes to claimed_at."""
        prior_claim = timezone.now()
        scrape = Scrape.objects.create(
            url="https://example.com/was-running",
            status="hold",
            claimed_at=prior_claim,  # simulate inconsistent prior state
            claimed_by="prior-runner",
        )

        _log_scrape_status(scrape.id, "hold")

        scrape.refresh_from_db()
        # The hold branch doesn't touch claimed_at at all. So a row
        # that arrived with a stale value still has the stale value —
        # the lease sweep is the system component that nulls it. This
        # test pins that _log_scrape_status doesn't bump it FORWARD
        # (which was the production bug).
        self.assertEqual(scrape.claimed_at, prior_claim)

    def test_running_still_bumps_claimed_at(self):
        """Non-hold non-terminal statuses (running, extracting, …) MUST
        still bump claimed_at — that's the active-runner heartbeat
        the lease sweep relies on. Regression guard against an
        overcorrection that would break Phase 2 lease recovery."""
        scrape = Scrape.objects.create(
            url="https://example.com/active",
            status="running",
            claimed_at=None,
            claimed_by="omarchy",
        )

        _log_scrape_status(scrape.id, "running")

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "running")
        self.assertIsNotNone(
            scrape.claimed_at,
            "running is an active-runner state; claimed_at heartbeat "
            "is what keeps the lease sweep from reaping it.",
        )

    def test_completed_clears_claim(self):
        """Existing contract — terminal status clears both fields.
        Pinned here to keep the matrix complete."""
        scrape = Scrape.objects.create(
            url="https://example.com/done",
            status="running",
            claimed_at=timezone.now(),
            claimed_by="omarchy",
        )

        _log_scrape_status(scrape.id, "completed")

        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "completed")
        self.assertIsNone(scrape.claimed_at)
        self.assertIsNone(scrape.claimed_by)
