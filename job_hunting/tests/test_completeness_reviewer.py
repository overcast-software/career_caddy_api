"""Tests for CompletenessReviewer and maybe_review_and_persist."""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost
from job_hunting.lib.parsers.completeness_reviewer import (
    CompletenessReviewer,
    ReviewDecision,
    maybe_review_and_persist,
)

User = get_user_model()

LONG_DESC = (
    "We are looking for a senior software engineer to join our distributed "
    "systems team. You'll build resilient pipelines, mentor juniors, and "
    "help shape our cloud architecture across AWS and GCP. Required: 5+ "
    "years of Python, strong Postgres skills, and experience with Kafka. "
    "Compensation: $180k-$220k base."
)


class TestReviewDecisionModel(TestCase):
    def test_accepts_valid(self):
        d = ReviewDecision(
            looks_like_job_description=True,
            confidence="high",
            reasoning="Real prose with responsibilities and requirements.",
        )
        self.assertTrue(d.looks_like_job_description)


class TestMaybeReviewAndPersist(TestCase):
    """Behavior of the helper that the parse_scrape hook calls.

    LLM is mocked everywhere — these tests verify the flag-flipping
    contract, not the model. CompletenessReviewer.review() is the
    boundary; tests below stub it directly so no real Anthropic/OpenAI
    call is ever made.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="rev", password="pw")
        self.company = Company.objects.create(name="Acme Corp")

    def _make_jp(self, *, description=LONG_DESC, complete=True):
        return JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=description,
            link="https://example.com/jobs/1",
            complete=complete,
            created_by=self.user,
        )

    @patch.object(CompletenessReviewer, "review")
    def test_pass_decision_leaves_complete_alone(self, mock_review):
        mock_review.return_value = ReviewDecision(
            looks_like_job_description=True,
            confidence="high",
            reasoning="Real job posting.",
        )
        jp = self._make_jp()
        decision = maybe_review_and_persist(jp, last_outcome="updated_stub")

        self.assertTrue(decision.looks_like_job_description)
        jp.refresh_from_db()
        self.assertTrue(jp.complete, "pass-decision must not flip complete")
        mock_review.assert_called_once()

    @patch.object(CompletenessReviewer, "review")
    def test_fail_decision_flips_complete_to_false(self, mock_review):
        mock_review.return_value = ReviewDecision(
            looks_like_job_description=False,
            confidence="medium",
            reasoning="Just a search-results page; no actual role.",
        )
        jp = self._make_jp()
        decision = maybe_review_and_persist(jp, last_outcome="updated_stub")

        self.assertFalse(decision.looks_like_job_description)
        jp.refresh_from_db()
        self.assertFalse(jp.complete, "fail-decision must flip complete to False")

    @patch.object(CompletenessReviewer, "review")
    def test_non_reviewable_outcome_skips_llm(self, mock_review):
        # last_outcome "duplicate" means the description wasn't touched —
        # no point paying for an LLM judgement.
        jp = self._make_jp()
        decision = maybe_review_and_persist(jp, last_outcome="duplicate")

        self.assertIsNone(decision)
        jp.refresh_from_db()
        self.assertTrue(jp.complete)
        mock_review.assert_not_called()

    @patch.object(CompletenessReviewer, "review")
    def test_empty_description_short_circuits_without_llm(self, mock_review):
        jp = self._make_jp(description="")
        decision = maybe_review_and_persist(jp, last_outcome="created")

        self.assertIsNotNone(decision)
        self.assertFalse(decision.looks_like_job_description)
        self.assertEqual(decision.confidence, "high")
        jp.refresh_from_db()
        self.assertFalse(jp.complete)
        mock_review.assert_not_called()

    @patch.object(CompletenessReviewer, "review")
    def test_short_description_falls_through_to_llm(self, mock_review):
        # Pre-gate fires only on truly empty descriptions. Anything with
        # content — even 9 chars — falls through to the LLM. The cost
        # of a false-reject (annoying) is worse than paying for the
        # call. CI failure 25583150427 caught this: a 19-char fixture
        # was getting auto-flipped without a fair read.
        mock_review.return_value = ReviewDecision(
            looks_like_job_description=True,
            confidence="low",
            reasoning="Short but plausible.",
        )
        jp = self._make_jp(description="Apply now")
        decision = maybe_review_and_persist(jp, last_outcome="created")

        self.assertTrue(decision.looks_like_job_description)
        jp.refresh_from_db()
        self.assertTrue(jp.complete)
        mock_review.assert_called_once()

    @patch.object(CompletenessReviewer, "review")
    @override_settings()  # placeholder; using monkeypatch on env below
    def test_disabled_via_env_skips_entirely(self, mock_review):
        import os
        os.environ["COMPLETENESS_REVIEWER_ENABLED"] = "false"
        try:
            jp = self._make_jp()
            decision = maybe_review_and_persist(jp, last_outcome="updated_stub")
            self.assertIsNone(decision)
            mock_review.assert_not_called()
            jp.refresh_from_db()
            self.assertTrue(jp.complete)
        finally:
            os.environ.pop("COMPLETENESS_REVIEWER_ENABLED", None)

    @patch.object(CompletenessReviewer, "review")
    def test_fail_on_already_incomplete_jp_is_idempotent(self, mock_review):
        # Reviewer rejects, but JP was already complete=False (e.g. user
        # marked incomplete just before). No spurious re-save.
        mock_review.return_value = ReviewDecision(
            looks_like_job_description=False,
            confidence="high",
            reasoning="Junk.",
        )
        jp = self._make_jp(complete=False)
        decision = maybe_review_and_persist(jp, last_outcome="updated_stub")

        self.assertFalse(decision.looks_like_job_description)
        jp.refresh_from_db()
        self.assertFalse(jp.complete)
