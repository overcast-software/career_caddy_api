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

    def test_get_agent_strips_openai_prefix(self):
        """pydantic-ai uses 'provider:model' specs (openai:gpt-4o-mini).
        OpenAIResponsesModel wants the bare model name — extractor must
        strip any provider prefix before constructing."""
        extractor = JobPostExtractor()
        captured = {}

        class _FakeModel:
            def __init__(self, name):
                captured["name"] = name

        class _FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

        env = {"CADDY_DEFAULT_MODEL": "openai:gpt-4o-mini"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "job_hunting.lib.parsers.job_post_extractor.OpenAIResponsesModel",
                _FakeModel,
            ),
            patch(
                "job_hunting.lib.parsers.job_post_extractor.Agent", _FakeAgent
            ),
        ):
            extractor.get_agent()
        self.assertEqual(captured["name"], "gpt-4o-mini")


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

    def test_non_stub_link_hit_is_duplicate_not_overwritten(self):
        """Non-stub JobPost at the same link: link the scrape, don't overwrite."""
        company = Company.objects.create(name="Acme Corp")
        rich_desc = "This is a full job posting. " * 20  # 100 words, non-stub
        existing = JobPost.objects.create(
            title="Original Title",
            company=company,
            link="https://example.com/job/1",
            description=rich_desc,
            location="Original HQ",
            created_by=self.user,
        )

        self.parsed_data.description = "Replacement description"
        self.parsed_data.location = "Replacement Location"

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(extractor.last_outcome, "duplicate")
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Original Title")
        self.assertEqual(existing.description, rich_desc)
        self.assertEqual(existing.location, "Original HQ")
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing.id)

    def test_stub_link_hit_is_upgraded(self):
        """Stub JobPost at the same link: overwrite its fields in place."""
        company = Company.objects.create(name="Acme Corp")
        existing = JobPost.objects.create(
            title="Stub Title",
            company=company,
            link="https://example.com/job/1",
            description="short",  # < 60 words → stub
            created_by=self.user,
        )

        self.parsed_data.title = "Real Title"
        self.parsed_data.description = (
            "Full description with enough words to clear the stub threshold. "
            * 10
        )
        self.parsed_data.location = "Remote"

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(extractor.last_outcome, "updated_stub")
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Real Title")
        self.assertIn("Full description", existing.description)
        self.assertEqual(existing.location, "Remote")
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing.id)

    def test_fresh_create_outcome(self):
        """No existing link match → outcome=created."""
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)
        self.assertEqual(extractor.last_outcome, "created")

    def test_duplicate_full_description_merges_empty_company(self):
        """Hold-poller scrape lands on existing full-description post with
        NULL company. Pre-merge code dropped the freshly-extracted company
        on the floor (`last_outcome="duplicate"` + early return). The
        merge call now backfills company_id while still leaving populated
        fields (title, description, location) untouched."""
        from job_hunting.models import JobPostDiscovery

        rich_desc = "This is a full job posting. " * 20
        existing = JobPost.objects.create(
            title="Original Title",
            company=None,  # the bug shape: full content, no company linkage
            link="https://example.com/job/1",
            description=rich_desc,
            created_by=self.user,
        )

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(extractor.last_outcome, "duplicate")
        existing.refresh_from_db()
        self.assertIsNotNone(
            existing.company_id,
            "duplicate-full-desc branch must merge empty company_id from "
            "incoming scrape — same shape as the cc_auto Microsoft regression",
        )
        self.assertEqual(existing.company.name, "Acme Corp")
        # Populated fields stay untouched.
        self.assertEqual(existing.title, "Original Title")
        self.assertEqual(existing.description, rich_desc)
        # And discovery for the scrape's owner is recorded.
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post=existing, user=self.user
            ).exists()
        )

    def test_extractor_records_discovery_for_scrape_owner(self):
        """Every successful process_evaluation must leave a
        JobPostDiscovery row tying the scrape's owner to the resulting
        JobPost. Without this, the user's only signal on a hold-poller
        post is `scrapes__created_by` — which we want to retire."""
        from job_hunting.models import JobPostDiscovery

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        job = JobPost.objects.get(title="Senior Engineer")
        disc = JobPostDiscovery.objects.filter(
            job_post=job, user=self.user
        ).first()
        self.assertIsNotNone(
            disc,
            "extractor must record discovery for scrape.created_by — "
            "discovery is the canonical visibility signal",
        )
        # Discovery source mirrors the scrape's source. The fixture
        # scrape uses the model default "manual"; an email-pipeline
        # hold-poller scrape would land "email" here instead.
        self.assertEqual(disc.source, "manual")

    def test_extractor_discovery_source_carries_scrape_source(self):
        """When a scrape carries an explicit source (e.g. 'email' from
        cc_auto's auto-scrape, or 'paste' from /scrapes/from-text),
        the resulting JobPostDiscovery.source must match — provenance
        flows through both records."""
        from job_hunting.models import JobPostDiscovery

        email_scrape = Scrape.objects.create(
            url="https://example.com/job/email",
            status="extracting",
            created_by=self.user,
            source="email",
        )
        extractor = JobPostExtractor()
        extractor.process_evaluation(email_scrape, self.parsed_data, user=self.user)

        job = JobPost.objects.get(link="https://example.com/job/email")
        disc = JobPostDiscovery.objects.get(job_post=job, user=self.user)
        self.assertEqual(disc.source, "email")

    def test_extractor_discovery_is_idempotent(self):
        """Repeat scrape from the same user shouldn't create duplicate
        discoveries (unique constraint catches it; we go through
        get_or_create)."""
        from job_hunting.models import JobPostDiscovery

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)
        scrape2 = Scrape.objects.create(
            url="https://example.com/job/1",  # same link → same JobPost
            status="extracting",
            created_by=self.user,
        )
        extractor2 = JobPostExtractor()
        extractor2.process_evaluation(scrape2, self.parsed_data, user=self.user)

        job = JobPost.objects.get(title="Senior Engineer")
        self.assertEqual(
            JobPostDiscovery.objects.filter(job_post=job, user=self.user).count(),
            1,
        )


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

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_paste_scrape_falls_back_to_job_content_when_llm_omits_description(
        self, mock_analyze
    ):
        """LLM returns description=None on a source='paste' scrape →
        scrape.job_content (the user's raw paste) becomes the description.
        Without this fallback we silently dropped the user's text on the
        floor (job-posts/1490 inbox bug)."""
        mock_analyze.return_value = ParsedJobData(
            title="Engineer", company_name="TestCo", description=None,
        )
        self.scrape.source = "paste"
        self.scrape.job_content = (
            "About the job: Build great things. Five years of experience required. "
            "Remote-friendly, full-time, immediate start."
        )
        self.scrape.save(update_fields=["source", "job_content"])

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        self.scrape.refresh_from_db()
        job = JobPost.objects.get(pk=self.scrape.job_post_id)
        self.assertIn("Build great things", job.description)

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_browser_scrape_does_not_fallback_to_html_dump(self, mock_analyze):
        """Non-paste scrapes must NOT use job_content as description —
        for browser-fetched scrapes job_content is HTML/page text with
        nav/footer noise, not description-clean."""
        mock_analyze.return_value = ParsedJobData(
            title="Engineer", company_name="TestCo", description=None,
        )
        self.scrape.source = "scrape"
        self.scrape.job_content = "<html><nav>menu</nav><body>raw</body></html>"
        self.scrape.save(update_fields=["source", "job_content"])

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        self.scrape.refresh_from_db()
        job = JobPost.objects.get(pk=self.scrape.job_post_id)
        self.assertIsNone(job.description)

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_force_noop_writes_distinct_status_note(self, mock_analyze):
        """force re-parse that produces no field changes surfaces a
        force_noop note so the frontend can flash 'nothing changed'
        instead of a generic success — otherwise a wasted parse looks
        identical to a useful one."""
        company = Company.objects.create(name="TestCo")
        existing = JobPost.objects.create(
            title="Engineer",
            company=company,
            description="Already complete.",
            created_by=self.user,
        )
        self.scrape.job_post_id = existing.id
        self.scrape.save(update_fields=["job_post_id"])
        # LLM extracts the same values that are already on the post.
        mock_analyze.return_value = ParsedJobData(
            title="Engineer", company_name="TestCo", description="Already complete.",
        )

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True, force=True)

        latest = self.scrape.scrape_statuses.order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertEqual(
            latest.note, f"force_noop: re-parse of JobPost #{existing.id} found no new fields",
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_duplicate_link_writes_duplicate_status_note(self, mock_analyze):
        """parse_scrape tags the completion note so the frontend can branch."""
        mock_analyze.return_value = ParsedJobData(
            title="Senior Engineer",
            company_name="Acme Corp",
            description="Replacement description",
        )
        company = Company.objects.create(name="Acme Corp")
        rich_desc = "This is a full job posting. " * 20
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            link="https://example.com/job/1",
            description=rich_desc,
            created_by=self.user,
        )

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing.id)
        latest = self.scrape.scrape_statuses.order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.note, f"duplicate: existing JobPost #{existing.id}")


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


class TestSourcePreservation(TestCase):
    """JobPost.source is provenance: assign on creation only.

    A later scrape upgrading or reparsing an existing JobPost must not
    overwrite the original origin. Regression for jp-1483 where an
    email-originated stub got flipped to source='scrape' once the
    hold-poller scraped the canonical URL the next day.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="provuser", password="pass")
        self.company = Company.objects.create(name="Optum")
        self.parsed = ParsedJobData(
            title="Senior Software Engineer",
            company_name="Optum",
            description="Build great things. " * 30,
            location="Remote",
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_persist_preserves_existing_source_on_stub_upgrade(self, mock_analyze):
        mock_analyze.return_value = self.parsed
        existing = JobPost.objects.create(
            title="Senior Software Engineer",
            company=self.company,
            link="https://example.com/job/1",
            description="stub",
            source="email",
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="page text " * 50,
            source="scrape",
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        existing.refresh_from_db()
        self.assertEqual(existing.source, "email")
        # Sanity: the stub-upgrade branch did fire (description was
        # replaced) so we know we're testing the right path.
        self.assertNotEqual(existing.description, "stub")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_persist_preserves_existing_source_on_force_reparse(self, mock_analyze):
        mock_analyze.return_value = self.parsed
        existing = JobPost.objects.create(
            title="Senior Software Engineer",
            company=self.company,
            link="https://example.com/job/1",
            description="prior full description " * 20,
            source="email",
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="page text " * 50,
            source="scrape",
            job_post=existing,
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True, force=True)
        existing.refresh_from_db()
        self.assertEqual(existing.source, "email")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_persist_preserves_existing_source_on_full_duplicate(self, mock_analyze):
        mock_analyze.return_value = self.parsed
        rich = "full description text " * 30
        existing = JobPost.objects.create(
            title="Senior Software Engineer",
            company=self.company,
            link="https://example.com/job/1",
            description=rich,
            source="email",
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="page text " * 50,
            source="scrape",
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        existing.refresh_from_db()
        self.assertEqual(existing.source, "email")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_persist_sets_source_on_cold_create(self, mock_analyze):
        mock_analyze.return_value = self.parsed
        scrape = Scrape.objects.create(
            url="https://example.com/cold-create",
            status="completed",
            job_content="page text " * 50,
            source="scrape",
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        self.assertIsNotNone(scrape.job_post_id)
        job = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(job.source, "scrape")
