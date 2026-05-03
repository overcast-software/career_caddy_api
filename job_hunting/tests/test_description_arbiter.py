"""Tests for DescriptionArbiter and the maybe_arbitrate_and_persist helper."""
import os
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost, Scrape
from job_hunting.lib.parsers.description_arbiter import (
    ArbitrationDecision,
    DescriptionArbiter,
    maybe_arbitrate_and_persist,
)
from job_hunting.lib.text_signals import jaccard_5gram

User = get_user_model()

THIN_TEXT = "Apply now. Sign in to save."
FULL_A = " ".join(["We are looking for an experienced software engineer to join our team."] * 10)
FULL_B = " ".join(["This role requires deep knowledge of distributed systems and cloud platforms."] * 10)


class TestJaccard5gram(TestCase):
    def test_identical_strings(self):
        s = "the quick brown fox jumps over the lazy dog"
        self.assertAlmostEqual(jaccard_5gram(s, s), 1.0)

    def test_empty_strings(self):
        self.assertEqual(jaccard_5gram("", ""), 0.0)
        self.assertEqual(jaccard_5gram("foo bar", ""), 0.0)

    def test_no_overlap(self):
        a = "apple banana cherry date elderberry"
        b = "zulu yankee xray whiskey victor"
        self.assertEqual(jaccard_5gram(a, b), 0.0)

    def test_partial_overlap(self):
        a = "we need engineers who understand cloud architecture and distributed systems"
        b = "we need engineers who understand cloud architecture and containerisation"
        score = jaccard_5gram(a, b)
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_short_string_below_ngram_size(self):
        # fewer than 5 tokens — should return 0.0 rather than raise
        self.assertEqual(jaccard_5gram("hello world", "hello world"), 0.0)


class TestArbitrationDecisionValidator(TestCase):
    def test_merge_without_merged_description_raises(self):
        with self.assertRaises(Exception):
            ArbitrationDecision(
                choice="merge",
                confidence="high",
                reasoning="both are great",
                merged_description=None,
            )

    def test_merged_description_cleared_for_non_merge(self):
        d = ArbitrationDecision(
            choice="keep_existing",
            confidence="high",
            reasoning="existing is better",
            merged_description="should be cleared",
        )
        self.assertIsNone(d.merged_description)

    def test_valid_use_new(self):
        d = ArbitrationDecision(
            choice="use_new",
            confidence="medium",
            reasoning="new is more complete",
            merged_description=None,
        )
        self.assertEqual(d.choice, "use_new")


class TestDescriptionArbiterCheapnessGate(TestCase):
    def test_near_identical_skips_llm(self):
        arbiter = DescriptionArbiter()
        # Duplicate text — should hit the cheapness gate without LLM
        with patch.object(arbiter, "_call_llm") as mock_llm:
            result = arbiter.arbitrate(
                title="SWE",
                company_name="Acme",
                existing_description=FULL_A,
                existing_link="https://example.com/job/1",
                existing_source="email",
                new_description=FULL_A,
                new_link="https://example.com/job/1",
                new_source="extension",
            )
        mock_llm.assert_not_called()
        self.assertEqual(result.choice, "keep_existing")
        self.assertEqual(result.confidence, "high")

    def test_different_descriptions_calls_llm(self):
        arbiter = DescriptionArbiter()
        llm_result = ArbitrationDecision(
            choice="use_new",
            confidence="high",
            reasoning="new is better",
            merged_description=None,
        )
        with patch.object(arbiter, "_call_llm", return_value=llm_result) as mock_llm:
            result = arbiter.arbitrate(
                title="SWE",
                company_name="Acme",
                existing_description=FULL_A,
                existing_link="https://example.com/job/1",
                existing_source="email",
                new_description=FULL_B,
                new_link="https://example.com/job/2",
                new_source="extension",
            )
        mock_llm.assert_called_once()
        self.assertEqual(result.choice, "use_new")


class TestMaybeArbitrateAndPersist(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="testuser", password="pw")
        cls.company = Company.objects.create(name="Arbiter Corp")

    def _make_job(self, description):
        return JobPost.objects.create(
            title="Test Job",
            company=self.company,
            description=description,
            source="email",
            link="https://example.com/job/1",
            created_by=self.user,
        )

    def _make_scrape(self, job=None):
        return Scrape.objects.create(
            url="https://example.com/job/1",
            source="extension",
            job_post=job,
            created_by=self.user,
        )

    def test_disabled_via_env_returns_none(self):
        job = self._make_job(FULL_A)
        scrape = self._make_scrape(job)
        with patch.dict(os.environ, {"INGEST_ARBITER_ENABLED": "false"}):
            result = maybe_arbitrate_and_persist(
                job_post=job,
                scrape=scrape,
                new_description=FULL_B,
                new_source="extension",
                new_link="https://example.com/job/1",
            )
        self.assertIsNone(result)

    def test_empty_existing_returns_none(self):
        job = self._make_job("")
        scrape = self._make_scrape(job)
        result = maybe_arbitrate_and_persist(
            job_post=job,
            scrape=scrape,
            new_description=FULL_B,
            new_source="extension",
            new_link="",
        )
        self.assertIsNone(result)

    def test_keep_existing_returns_existing(self):
        job = self._make_job(FULL_A)
        scrape = self._make_scrape(job)
        decision = ArbitrationDecision(
            choice="keep_existing",
            confidence="high",
            reasoning="existing is fine",
            merged_description=None,
        )
        arbiter_mock = MagicMock()
        arbiter_mock.arbitrate.return_value = decision
        arbiter_mock.model_spec = "openai:gpt-4o-mini"
        with patch(
            "job_hunting.lib.parsers.description_arbiter.DescriptionArbiter",
            return_value=arbiter_mock,
        ):
            result = maybe_arbitrate_and_persist(
                job_post=job,
                scrape=scrape,
                new_description=FULL_B,
                new_source="extension",
                new_link="",
            )
        self.assertEqual(result, FULL_A)

    def test_use_new_returns_new_description(self):
        job = self._make_job(FULL_A)
        scrape = self._make_scrape(job)
        decision = ArbitrationDecision(
            choice="use_new",
            confidence="high",
            reasoning="new is more complete",
            merged_description=None,
        )
        arbiter_mock = MagicMock()
        arbiter_mock.arbitrate.return_value = decision
        arbiter_mock.model_spec = "openai:gpt-4o-mini"
        with patch(
            "job_hunting.lib.parsers.description_arbiter.DescriptionArbiter",
            return_value=arbiter_mock,
        ):
            result = maybe_arbitrate_and_persist(
                job_post=job,
                scrape=scrape,
                new_description=FULL_B,
                new_source="extension",
                new_link="",
            )
        self.assertEqual(result, FULL_B)

    def test_merge_returns_merged_text(self):
        merged = FULL_A + " " + FULL_B
        job = self._make_job(FULL_A)
        scrape = self._make_scrape(job)
        decision = ArbitrationDecision(
            choice="merge",
            confidence="medium",
            reasoning="both have unique content",
            merged_description=merged,
        )
        arbiter_mock = MagicMock()
        arbiter_mock.arbitrate.return_value = decision
        arbiter_mock.model_spec = "openai:gpt-4o-mini"
        with patch(
            "job_hunting.lib.parsers.description_arbiter.DescriptionArbiter",
            return_value=arbiter_mock,
        ):
            result = maybe_arbitrate_and_persist(
                job_post=job,
                scrape=scrape,
                new_description=FULL_B,
                new_source="extension",
                new_link="",
            )
        self.assertEqual(result, merged)

    def test_llm_error_persists_low_confidence_row(self):
        from job_hunting.models.job_post_description_decision import (
            JobPostDescriptionDecision,
        )

        job = self._make_job(FULL_A)
        scrape = self._make_scrape(job)
        arbiter_mock = MagicMock()
        arbiter_mock.arbitrate.side_effect = RuntimeError("LLM timeout")
        arbiter_mock.model_spec = "openai:gpt-4o-mini"
        with patch(
            "job_hunting.lib.parsers.description_arbiter.DescriptionArbiter",
            return_value=arbiter_mock,
        ):
            result = maybe_arbitrate_and_persist(
                job_post=job,
                scrape=scrape,
                new_description=FULL_B,
                new_source="extension",
                new_link="",
            )
        # Falls back to keep_existing
        self.assertEqual(result, FULL_A)
        # Audit row must still be written
        rows = JobPostDescriptionDecision.objects.filter(job_post=job)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().confidence, "low")

    def test_audit_row_written_on_success(self):
        from job_hunting.models.job_post_description_decision import (
            JobPostDescriptionDecision,
        )

        job = self._make_job(FULL_A)
        scrape = self._make_scrape(job)
        decision = ArbitrationDecision(
            choice="use_new",
            confidence="high",
            reasoning="new is better",
            merged_description=None,
        )
        arbiter_mock = MagicMock()
        arbiter_mock.arbitrate.return_value = decision
        arbiter_mock.model_spec = "openai:gpt-4o-mini"
        with patch(
            "job_hunting.lib.parsers.description_arbiter.DescriptionArbiter",
            return_value=arbiter_mock,
        ):
            maybe_arbitrate_and_persist(
                job_post=job,
                scrape=scrape,
                new_description=FULL_B,
                new_source="extension",
                new_link="",
            )
        rows = JobPostDescriptionDecision.objects.filter(job_post=job)
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertEqual(row.choice, "use_new")
        self.assertEqual(row.confidence, "high")
        self.assertEqual(row.new_source, "extension")
        self.assertEqual(row.existing_source, "email")
