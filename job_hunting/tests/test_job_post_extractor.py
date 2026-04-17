import os
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost, Scrape, ScrapeProfile
from job_hunting.lib.parsers.job_post_extractor import (
    JobPostExtractor,
    ParsedJobData,
    _update_scrape_profile,
    parse_scrape,
)

User = get_user_model()


class TestModelResolution(TestCase):
    """Test env-var-based model selection in JobPostExtractor."""

    def test_default_model(self):
        extractor = JobPostExtractor()
        with patch.dict(os.environ, {}, clear=True):
            name = extractor._resolve_model_name()
        self.assertEqual(name, "gpt-4o")

    def test_role_specific_env_var(self):
        extractor = JobPostExtractor()
        with patch.dict(os.environ, {"JOB_PARSER_MODEL": "gpt-4o-mini"}, clear=True):
            name = extractor._resolve_model_name()
        self.assertEqual(name, "gpt-4o-mini")

    def test_fallback_env_var(self):
        extractor = JobPostExtractor()
        with patch.dict(os.environ, {"CADDY_DEFAULT_MODEL": "gpt-4o-mini"}, clear=True):
            name = extractor._resolve_model_name()
        self.assertEqual(name, "gpt-4o-mini")

    def test_role_specific_beats_fallback(self):
        extractor = JobPostExtractor()
        env = {"JOB_PARSER_MODEL": "gpt-4o", "CADDY_DEFAULT_MODEL": "gpt-4o-mini"}
        with patch.dict(os.environ, env, clear=True):
            name = extractor._resolve_model_name()
        self.assertEqual(name, "gpt-4o")


class TestProcessEvaluation(TestCase):
    """Test JobPostExtractor.process_evaluation creates/links records correctly."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="extracting",
            created_by=self.user,
        )
        self.parsed_data = ParsedJobData(
            title="Senior Engineer",
            company_name="Acme Corp",
            company_display_name="Acme",
            description="Build things.",
            location="Remote",
            remote=True,
        )

    def test_creates_company_and_job(self):
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        company = Company.objects.get(name="Acme Corp")
        self.assertEqual(company.display_name, "Acme")

        job = JobPost.objects.get(title="Senior Engineer", company=company)
        self.assertEqual(job.link, "https://example.com/job/1")
        self.assertEqual(job.created_by, self.user)

        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, job.id)
        self.assertEqual(self.scrape.company_id, company.id)

    def test_company_is_shared_resource(self):
        """Company has no user scoping — same name always returns same record."""
        user2 = User.objects.create_user(username="otheruser", password="pass")
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        scrape2 = Scrape.objects.create(
            url="https://example.com/job/2", status="extracting", created_by=user2,
        )
        extractor2 = JobPostExtractor()
        extractor2.process_evaluation(scrape2, self.parsed_data, user=user2)

        self.assertEqual(Company.objects.filter(name="Acme Corp").count(), 1)

    def test_existing_job_by_link_not_duplicated(self):
        company = Company.objects.create(name="Acme Corp")
        existing_job = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            link="https://example.com/job/1",
            created_by=self.user,
        )

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(JobPost.objects.filter(link="https://example.com/job/1").count(), 1)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing_job.id)


class TestParseScrape(TestCase):
    """Test parse_scrape orchestration function."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="Some job posting content here",
            created_by=self.user,
        )
        self.mock_parsed = ParsedJobData(
            title="Engineer",
            company_name="TestCo",
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_happy_path(self, mock_analyze):
        mock_analyze.return_value = self.mock_parsed

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        self.scrape.refresh_from_db()
        self.assertIsNotNone(self.scrape.job_post_id)

        job = JobPost.objects.get(pk=self.scrape.job_post_id)
        self.assertEqual(job.title, "Engineer")
        self.assertEqual(job.created_by, self.user)

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_already_extracted_skips(self, mock_analyze):
        job = JobPost.objects.create(
            title="Existing",
            created_by=self.user,
        )
        self.scrape.job_post_id = job.id
        self.scrape.save(update_fields=["job_post_id"])

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        mock_analyze.assert_not_called()

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_no_content_skips(self, mock_analyze):
        self.scrape.job_content = ""
        self.scrape.save(update_fields=["job_content"])

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        mock_analyze.assert_not_called()

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_ai_failure_sets_failed_status(self, mock_analyze):
        mock_analyze.side_effect = RuntimeError("LLM exploded")

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.status, "failed")
        self.assertIsNone(self.scrape.job_post_id)

    def test_nonexistent_scrape_no_error(self):
        parse_scrape(999999, user_id=self.user.id, sync=True)

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_falls_back_to_scrape_created_by(self, mock_analyze):
        mock_analyze.return_value = self.mock_parsed

        parse_scrape(self.scrape.id, user_id=None, sync=True)

        self.scrape.refresh_from_db()
        job = JobPost.objects.get(pk=self.scrape.job_post_id)
        self.assertEqual(job.created_by, self.user)


class TestUpdateScrapeProfile(TestCase):
    """_update_scrape_profile records successes AND failures, auto-demotes Tier 0."""

    def setUp(self):
        self.user = User.objects.create_user(username="profileuser", password="pass")
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="x" * 100,
            created_by=self.user,
        )

    def test_success_creates_profile(self):
        _update_scrape_profile(self.scrape, self.user, success=True)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.scrape_count, 1)
        self.assertEqual(profile.failure_count, 0)
        self.assertEqual(profile.success_rate, 1.0)
        self.assertIsNotNone(profile.last_success_at)

    def test_failure_creates_profile_with_zero_rate(self):
        _update_scrape_profile(self.scrape, self.user, success=False)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.failure_count, 1)
        self.assertEqual(profile.success_rate, 0.0)
        self.assertIsNone(profile.last_success_at)

    def test_failures_pull_success_rate_down(self):
        _update_scrape_profile(self.scrape, self.user, success=True)
        _update_scrape_profile(self.scrape, self.user, success=False)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.scrape_count, 2)
        self.assertEqual(profile.failure_count, 1)
        self.assertAlmostEqual(profile.success_rate, 0.5)

    def test_tier0_miss_bumps_counter(self):
        _update_scrape_profile(self.scrape, self.user, success=True, tier0_hit=False)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.tier0_miss_count, 1)

    def test_tier0_hit_does_not_bump_miss(self):
        _update_scrape_profile(self.scrape, self.user, success=True, tier0_hit=True)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.tier0_miss_count, 0)

    def test_auto_demotes_after_repeated_misses(self):
        # Seed with 6 tier0 misses on an existing auto-tier profile
        for _ in range(6):
            _update_scrape_profile(self.scrape, self.user, success=True, tier0_hit=False)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.tier0_miss_count, 6)
        self.assertEqual(profile.preferred_tier, "1")

    def test_does_not_demote_when_misses_below_threshold(self):
        for _ in range(4):
            _update_scrape_profile(self.scrape, self.user, success=True, tier0_hit=False)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.preferred_tier, "auto")

    def test_does_not_demote_when_explicit_tier_set(self):
        _update_scrape_profile(self.scrape, self.user, success=True)
        ScrapeProfile.objects.filter(hostname="example.com").update(preferred_tier="0")
        for _ in range(6):
            _update_scrape_profile(self.scrape, self.user, success=True, tier0_hit=False)
        profile = ScrapeProfile.objects.get(hostname="example.com")
        self.assertEqual(profile.preferred_tier, "0")
