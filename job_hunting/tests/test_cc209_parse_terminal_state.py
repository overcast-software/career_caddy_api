"""CC-209: a (re-)parse must leave the Scrape row in a coherent terminal state.

Found during the CC-199 reclaim: re-parsing a scrape that had previously
failed left it as ``status=completed`` + ``job_post_id=NULL`` +
``failure_reason=<the OLD 429>``. All three at once is incoherent —
``completed`` says success, the NULL FK says nothing was produced, and the
stale reason records a failure that was no longer real. The row then hid
from every ``status=failed`` recovery sweep, and three reclaim rounds were
spent treating the stale 429 as live.

Contract pinned here:

- A parse starts clean: the previous attempt's ``failure_reason`` never
  survives into this run's terminal state.
- ``completed`` implies a linked JobPost. A run that reports success but
  links nothing lands ``failed`` with a reason naming that, never
  ``completed``-with-NULL.
- The terminal write is one UPDATE: ``_log_scrape_status("completed")``
  clears ``failure_reason`` in the same statement that flips the status.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from job_hunting.lib.parsers.job_post_extractor import (
    JobPostExtractor,
    parse_scrape,
)
from job_hunting.lib.scraper import _log_scrape_status
from job_hunting.models import JobPost, Scrape


User = get_user_model()

STALE_REASON = (
    "parse_scrape exception: ModelHTTPError 429 insufficient_quota (gpt-4o)"
)


class _ReviewerOff:
    """Keep the CompletenessReviewer out of these runs — it is a separate
    gate with its own tests, and it would otherwise decide the terminal
    status these tests are pinning."""

    def setUp(self):
        super().setUp()
        p = patch(
            "job_hunting.lib.parsers.completeness_reviewer.maybe_review_and_persist",
            return_value=None,
        )
        p.start()
        self.addCleanup(p.stop)


class TestReparseTerminalStateIsCoherent(_ReviewerOff, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="cc209", password="pw")
        # A scrape exactly as the CC-199 reclaim found it: a prior failed
        # attempt left its reason on the row.
        self.scrape = Scrape.objects.create(
            url="https://example.com/jobs/1",
            job_content="x" * 300,
            status="failed",
            failure_reason=STALE_REASON,
            created_by=self.user,
            source="extension",
        )

    def _parse_links_a_jobpost(self, _self, scrape, user=None, force=False):
        jp = JobPost.objects.create(title="Linked by re-parse", created_by=self.user)
        Scrape.objects.filter(pk=scrape.pk).update(job_post_id=jp.id)
        return True

    def test_successful_reparse_clears_the_stale_failure_reason(self):
        with patch.object(
            JobPostExtractor, "parse", autospec=True,
            side_effect=self._parse_links_a_jobpost,
        ):
            parse_scrape(self.scrape.id, user_id=self.user.id, sync=True, force=True)

        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.status, "completed")
        self.assertIsNotNone(self.scrape.job_post_id)
        self.assertIsNone(
            self.scrape.failure_reason,
            "a completed scrape must not carry a previous attempt's failure",
        )

    def test_success_without_a_linked_jobpost_lands_failed_not_completed(self):
        # The exact CC-199 shape: the extractor says "success" but nothing
        # was linked. Before the fix this was written as completed + NULL.
        with patch.object(
            JobPostExtractor, "parse", autospec=True, return_value=True,
        ):
            parse_scrape(self.scrape.id, user_id=self.user.id, sync=True, force=True)

        self.scrape.refresh_from_db()
        self.assertIsNone(self.scrape.job_post_id)
        self.assertEqual(
            self.scrape.status, "failed",
            "completed must imply a linked JobPost",
        )
        self.assertIn("no JobPost was linked", self.scrape.failure_reason or "")
        self.assertNotIn("429", self.scrape.failure_reason or "")
        latest = self.scrape.scrape_statuses.order_by("-id").first()
        self.assertIn("no_job_post", latest.note or "")

    def test_failed_reparse_replaces_rather_than_keeps_the_stale_reason(self):
        with patch.object(
            JobPostExtractor, "parse", autospec=True,
            side_effect=RuntimeError("fresh failure this run"),
        ):
            parse_scrape(self.scrape.id, user_id=self.user.id, sync=True, force=True)

        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.status, "failed")
        self.assertIn("fresh failure this run", self.scrape.failure_reason)
        self.assertNotIn("429", self.scrape.failure_reason)

    def test_stale_reason_is_gone_while_the_run_is_in_flight(self):
        # A poll during extraction must not see last week's error either.
        seen = {}

        def capture(_self, scrape, user=None, force=False):
            seen["reason"] = Scrape.objects.get(pk=scrape.pk).failure_reason
            return self._parse_links_a_jobpost(_self, scrape, user=user, force=force)

        with patch.object(JobPostExtractor, "parse", autospec=True, side_effect=capture):
            parse_scrape(self.scrape.id, user_id=self.user.id, sync=True, force=True)

        self.assertIn("reason", seen)
        self.assertIsNone(seen["reason"])


class TestCompletedWriteClearsFailureReasonAtomically(TestCase):
    def test_log_scrape_status_completed_nulls_failure_reason(self):
        user = User.objects.create_user(username="cc209b", password="pw")
        scrape = Scrape.objects.create(
            url="https://example.com/jobs/2",
            status="failed",
            failure_reason=STALE_REASON,
            created_by=user,
        )
        _log_scrape_status(scrape.id, "completed", note="ok")
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "completed")
        self.assertIsNone(scrape.failure_reason)

    def test_failed_write_without_a_reason_preserves_the_richer_one(self):
        # The pre-existing invariant from the failure_reason surface: a
        # failed write with failure_reason=None leaves a richer reason
        # written earlier in the same run alone. Pinned so CC-209's
        # clearing rule is scoped to `completed` only.
        user = User.objects.create_user(username="cc209c", password="pw")
        scrape = Scrape.objects.create(
            url="https://example.com/jobs/3",
            status="extracting",
            failure_reason="Extraction returned placeholder title: 'N/A'",
            created_by=user,
        )
        _log_scrape_status(scrape.id, "failed", note="Extraction failed", failure_reason=None)
        scrape.refresh_from_db()
        self.assertEqual(scrape.status, "failed")
        self.assertIn("placeholder", scrape.failure_reason)
