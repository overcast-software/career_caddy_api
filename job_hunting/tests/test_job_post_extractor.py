import os
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import (
    Company,
    JobPost,
    JobPostOverwriteDecision,
    Scrape,
    ScrapeProfile,
)
from job_hunting.models.job_post_dedupe import source_trust
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
        self.assertEqual(name, "openai:gpt-4o")

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

    def test_anthropic_prefix_routes_to_anthropic_model(self):
        """Tier2/3 escalation passes 'anthropic:claude-haiku-4-5' /
        'anthropic:claude-sonnet-4-6' through llm-extract. The dispatch
        must construct an AnthropicModel with the bare name — not pass
        'anthropic:...' through to OpenAIResponsesModel, which 400s with
        'model_not_found' (incident: scrape #237 → jp 1550 LinkedIn,
        2026-04-30)."""
        extractor = JobPostExtractor()
        captured = {}

        class _FakeAnthropicModel:
            def __init__(self, name):
                captured["name"] = name
                captured["cls"] = "AnthropicModel"

        class _FakeOpenAIResponsesModel:
            def __init__(self, name):
                captured["name"] = name
                captured["cls"] = "OpenAIResponsesModel"

        class _FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

        with (
            patch(
                "pydantic_ai.models.anthropic.AnthropicModel", _FakeAnthropicModel
            ),
            patch(
                "job_hunting.lib.parsers.job_post_extractor.OpenAIResponsesModel",
                _FakeOpenAIResponsesModel,
            ),
            patch(
                "job_hunting.lib.parsers.job_post_extractor.Agent", _FakeAgent
            ),
        ):
            extractor._build_agent_for_model("anthropic:claude-haiku-4-5")
        self.assertEqual(captured["cls"], "AnthropicModel")
        self.assertEqual(captured["name"], "claude-haiku-4-5")

    def test_build_agent_rejects_bare_name(self):
        """Bare model names (no provider:) must raise ValueError. The
        dispatch function is the only place env values land in pydantic-
        ai, so this is the chokepoint that enforces the explicit-prefix
        policy. Prevents the silent OpenAI-misroute that produced the
        Tier2 incident on 2026-04-30."""
        extractor = JobPostExtractor()
        with self.assertRaises(ValueError) as ctx:
            extractor._build_agent_for_model("gpt-4o")
        self.assertIn("provider:model", str(ctx.exception))

    def test_build_agent_rejects_unknown_provider(self):
        extractor = JobPostExtractor()
        with self.assertRaises(ValueError) as ctx:
            extractor._build_agent_for_model("cohere:command-r")
        self.assertIn("Unknown provider", str(ctx.exception))

    def test_get_model_name_recognizes_anthropic(self):
        """AiUsage rows must label anthropic models as 'anthropic:<name>',
        not str(model). Mirrors the OpenAI/Ollama branches so pricing.py
        lookups succeed."""
        extractor = JobPostExtractor()

        class _FakeAnthropicModel:
            model_name = "claude-haiku-4-5"

        _FakeAnthropicModel.__name__ = "AnthropicModel"

        class _FakeAgent:
            model = _FakeAnthropicModel()

        extractor.agent = _FakeAgent()
        self.assertEqual(extractor._get_model_name(), "anthropic:claude-haiku-4-5")


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
        """Incomplete JobPost at the same link: overwrite its fields in place
        and flip complete=True after the upgrade."""
        company = Company.objects.create(name="Acme Corp")
        existing = JobPost.objects.create(
            title="Stub Title",
            company=company,
            link="https://example.com/job/1",
            description="short",
            complete=False,
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
        self.assertTrue(existing.complete, "upgrade flips complete=True")
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing.id)

    def test_fresh_create_outcome(self):
        """No existing link match → outcome=created."""
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)
        self.assertEqual(extractor.last_outcome, "created")

    def test_canonical_link_hit_upgrades_email_stub_at_redirected_url(self):
        """jp 1918 / jp 1922 incident regression: an extension push at the
        canonical /jobs/view/<id>/ URL must dedup against an email-stub
        whose raw link is /comm/jobs/view/<id>/ (the form LinkedIn email
        delivers and then redirects). Pre-fix link_hit lookup only
        compared `link` for equality, missing the canonical_link match,
        so the upgrade-the-stub branch was bypassed and parse_scrape
        forked a new JobPost via the (title, company) get_or_create
        cold path. With the canonical_link OR-leg in place the existing
        stub gets upgraded in place — no fork."""
        stub = JobPost.objects.create(
            title="Senior Software Engineer, Security",
            company=None,  # email stub: title only, no company yet
            link="https://www.linkedin.com/comm/jobs/view/4370923838/",
            # Canonical form post-2026-05-27 strips the trailing slash so
            # stage-1 dedup matches /jobs/view/<id> with or without the
            # slash; setting it explicitly here to match what
            # canonicalize_link would have produced on save().
            canonical_link="https://www.linkedin.com/jobs/view/4370923838",
            source="email",
            complete=False,
            created_by=self.user,
        )

        # Extension's resent scrape lands at the redirected canonical URL.
        self.scrape.url = "https://www.linkedin.com/jobs/view/4370923838/"
        self.scrape.source = "extension"
        self.scrape.save()
        self.parsed_data.title = "Senior Software Engineer, Security"
        self.parsed_data.company_name = "Teleport"
        self.parsed_data.company_display_name = "Teleport"
        self.parsed_data.description = (
            "Unified Identity Securing Classic and AI Infrastructure. " * 20
        )

        extractor = JobPostExtractor()
        extractor.process_evaluation(
            self.scrape, self.parsed_data, user=self.user,
        )

        self.assertEqual(
            JobPost.objects.count(), 1,
            "must upgrade the email stub in place, not fork a new JP",
        )
        stub.refresh_from_db()
        self.assertTrue(stub.complete, "upgrade flips complete=True")
        self.assertIn("Unified Identity", stub.description or "")
        self.assertIsNotNone(stub.company_id, "company linked on upgrade")
        self.assertEqual(stub.company.name, "Teleport")
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, stub.id)

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

    def test_cold_path_thin_existing_upgrades_from_full(self):
        """jp 1603 incident: an extension scrape from welcometothejungle.com
        deduped to an email-sourced LinkedIn JP via title+company match.
        The existing JP had only the LinkedIn off-platform-managed page
        chrome (~32 words) as its description; the new scrape carried the
        full role description from the aggregator. Layer 1 mirrors the
        link-hit-thin branch into the cold path so the rich description
        wins. Without this, get_or_create returns the existing match and
        silently drops the new defaults."""
        company = Company.objects.create(name="Acme Corp")
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            link="https://email-sourced.example.com/job/old",
            description="Apply Save Sign in About",
            location="Stale Location",
            complete=False,
            created_by=self.user,
        )

        rich_desc = (
            "We are looking for a Senior Engineer to join our team. "
            "Responsibilities include building scalable services. "
            "Required skills: Python, Django, distributed systems. "
        ) * 6  # ~96 words → clears STUB_MIN_WORDS=60

        self.parsed_data.description = rich_desc
        self.parsed_data.location = "Remote"

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(extractor.last_outcome, "updated_stub_via_fingerprint")
        existing.refresh_from_db()
        self.assertEqual(existing.description, rich_desc)
        self.assertEqual(existing.location, "Remote")
        # Link is in _NO_OVERWRITE_FIELDS-adjacent territory: it isn't in
        # job_defaults at all (set only on cold-create), so the original
        # email-attested link survives.
        self.assertEqual(existing.link, "https://email-sourced.example.com/job/old")
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing.id)

    def test_cold_path_full_existing_keeps_description(self):
        """Both descriptions non-thin: fall through to duplicate_via_fingerprint
        (NULL-fill only, no overwrite). Layer 2 will arbitrate this case;
        Layer 1 deliberately preserves the existing populated description."""
        company = Company.objects.create(name="Acme Corp")
        rich_desc = "Full original description with substantial content. " * 20  # ~140 words
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            link="https://email-sourced.example.com/job/old",
            description=rich_desc,
            created_by=self.user,
        )

        new_desc = "Different rich description from a second source. " * 20  # ~140 words
        self.parsed_data.description = new_desc

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(extractor.last_outcome, "duplicate_via_fingerprint")
        existing.refresh_from_db()
        # Description deliberately preserved — Layer 2 is the layer that
        # decides between two non-thin descriptions.
        self.assertEqual(existing.description, rich_desc)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.job_post_id, existing.id)

    def test_cold_path_created_outcome_unchanged(self):
        """Fresh title+company → get_or_create returns created=True →
        last_outcome stays at the default 'created' set at the top of
        process_evaluation. No new fingerprint sentinel for the create
        path (the create itself is the upgrade)."""
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)
        # Default outcome from process_evaluation entry point.
        self.assertEqual(extractor.last_outcome, "created")
        self.assertTrue(JobPost.objects.filter(title="Senior Engineer").exists())

    def test_cold_path_thin_existing_thin_new_falls_to_duplicate(self):
        """Both descriptions thin: the upgrade gate requires the NEW desc
        to clear STUB_MIN_WORDS. If it doesn't, fall through to
        duplicate_via_fingerprint (NULL-fill) — never overwrite a stub
        with another stub."""
        company = Company.objects.create(name="Acme Corp")
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            link="https://email-sourced.example.com/job/old",
            description="five word stub description here",
            complete=False,
            created_by=self.user,
        )

        self.parsed_data.description = "also a tiny new description"  # 5 words → thin

        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self.parsed_data, user=self.user)

        self.assertEqual(extractor.last_outcome, "duplicate_via_fingerprint")
        existing.refresh_from_db()
        self.assertEqual(existing.description, "five word stub description here")

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


class TestPrefillExtraction(TestCase):
    """The extension-prefill fast path. When the browser ran per-host
    job_data selectors and posted the resulting dict on /scrapes/from-
    text/, JobPostExtractor.parse() builds ParsedJobData directly from
    Scrape.extension_prefill and skips the LLM entirely — but only when
    title + company_name clear the plausibility floor.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="prefillu", password="pw")
        self.base = dict(
            url="https://www.linkedin.com/jobs/view/1/",
            status="pending",
            job_content="Some job posting content here",
            created_by=self.user,
        )

    def test_hit_returns_parsed_data(self):
        scrape = Scrape.objects.create(
            extension_prefill={
                "title": "Senior Engineer",
                "company_name": "Acme",
                "location": "Remote",
            },
            **self.base,
        )
        extractor = JobPostExtractor()
        data, attempted = extractor._try_prefill_extraction(scrape)
        self.assertTrue(attempted)
        self.assertIsNotNone(data)
        self.assertEqual(data.title, "Senior Engineer")
        self.assertEqual(data.company_name, "Acme")
        self.assertEqual(data.location, "Remote")

    def test_missing_company_misses(self):
        scrape = Scrape.objects.create(
            extension_prefill={"title": "Senior Engineer"},
            **self.base,
        )
        data, attempted = JobPostExtractor()._try_prefill_extraction(scrape)
        self.assertTrue(attempted)
        self.assertIsNone(data)

    def test_short_title_misses_plausibility_floor(self):
        scrape = Scrape.objects.create(
            extension_prefill={"title": "X", "company_name": "Acme"},
            **self.base,
        )
        data, attempted = JobPostExtractor()._try_prefill_extraction(scrape)
        self.assertTrue(attempted)
        self.assertIsNone(data)

    def test_no_prefill_not_attempted(self):
        scrape = Scrape.objects.create(extension_prefill=None, **self.base)
        data, attempted = JobPostExtractor()._try_prefill_extraction(scrape)
        self.assertFalse(attempted)
        self.assertIsNone(data)

    def test_company_field_alias_accepted(self):
        """Some extractors emit `company` instead of `company_name`; the
        selector dict shape varies and the extension is a thin pass-
        through. Accept the legacy alias so a per-host config can use
        either key."""
        scrape = Scrape.objects.create(
            extension_prefill={"title": "Senior Engineer", "company": "Acme"},
            **self.base,
        )
        data, attempted = JobPostExtractor()._try_prefill_extraction(scrape)
        self.assertTrue(attempted)
        self.assertIsNotNone(data)
        self.assertEqual(data.company_name, "Acme")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_parse_skips_llm_on_prefill_hit(self, mock_analyze):
        """End-to-end: parse() sees prefill, skips analyze_with_ai
        entirely. The LLM cost is what this whole feature exists to
        eliminate on dogfooded hosts."""
        scrape = Scrape.objects.create(
            extension_prefill={
                "title": "Senior Engineer",
                "company_name": "Acme",
            },
            **self.base,
        )
        extractor = JobPostExtractor()
        extractor.parse(scrape, user=self.user)
        mock_analyze.assert_not_called()
        self.assertTrue(extractor.last_prefill_hit)
        scrape.refresh_from_db()
        self.assertIsNotNone(scrape.job_post_id)
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(jp.title, "Senior Engineer")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_parse_falls_through_to_llm_when_prefill_misses(self, mock_analyze):
        """A prefill present but missing required fields falls through
        to Tier 0 / LLM. last_prefill_hit records the miss so admin /
        logs can surface the rate."""
        mock_analyze.return_value = ParsedJobData(
            title="LLM Recovered", company_name="Acme"
        )
        scrape = Scrape.objects.create(
            extension_prefill={"title": "Senior Engineer"},
            **self.base,
        )
        extractor = JobPostExtractor()
        extractor.parse(scrape, user=self.user)
        mock_analyze.assert_called_once()
        self.assertFalse(extractor.last_prefill_hit)


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
    def test_extension_scrape_falls_back_to_job_content_when_llm_omits_description(
        self, mock_analyze
    ):
        """Extension scrapes use source='extension'. When LLM returns
        description=None, job_content (the browser-captured text) becomes
        the fallback description — same as paste. Without this, the rich
        capture from the extension would be silently lost."""
        mock_analyze.return_value = ParsedJobData(
            title="Engineer", company_name="TestCo", description=None,
        )
        self.scrape.source = "extension"
        self.scrape.job_content = (
            "Real description text " * 20  # > STUB_MIN_WORDS
        )
        self.scrape.save(update_fields=["source", "job_content"])

        parse_scrape(self.scrape.id, user_id=self.user.id, sync=True)

        self.scrape.refresh_from_db()
        job = JobPost.objects.get(pk=self.scrape.job_post_id)
        self.assertIn("Real description text", job.description)

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
    def test_persist_preserves_existing_source_on_same_trust_stub_upgrade(self, mock_analyze):
        # Source preservation invariant — kept narrow to the same-trust
        # case (email vs email_direct, both trust 20). The cross-trust
        # case (e.g. scrape onto email) is now an OVERWRITE, covered in
        # test_extension_overwrites_email_jobpost.
        mock_analyze.return_value = self.parsed
        existing = JobPost.objects.create(
            title="Senior Software Engineer",
            company=self.company,
            link="https://example.com/job/1",
            description="stub",
            source="email",
            complete=False,
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            job_content="page text " * 50,
            source="email_direct",
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
    def test_persist_preserves_existing_source_on_same_trust_full_duplicate(self, mock_analyze):
        # Source preservation invariant — kept narrow to the same-trust
        # case (email vs email_direct, both trust 20). The cross-trust
        # case is now an OVERWRITE, covered in
        # test_extension_overwrites_email_jobpost_full_description.
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
            source="email_direct",
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


class TestClosedBannerHallucinationGuard(TestCase):
    """Defends against the jp 1550 incident (2026-05-01).

    A linkedin extraction_hints blob instructed Tier1Mini to lead the
    description with "[CLOSED — applications no longer accepted]" on
    every post. The model dutifully synthesized that prefix on an
    ACTIVE Lululemon Senior Cybersecurity Engineer posting; the
    text_signals regex matched the synthesized prefix and flipped
    posting_status to "closed". Two-channel guard:
      1. closed_evidence must be a verbatim substring of job_content
      2. _strip_closed_banner_prefix removes synthetic [CLOSED ...]
         from the description regardless of posting_status outcome
    """

    def setUp(self):
        self.user = User.objects.create_user(username="hallu", password="pass")
        self.company = Company.objects.create(name="Lululemon")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_unsubstantiated_closed_evidence_is_discarded(self, mock_analyze):
        # Page text is unambiguously active — no closed phrase anywhere.
        active_page = (
            "About the job. We are hiring. Apply now. Reposted 6 days ago. "
            "Over 100 people clicked apply. Senior Cybersecurity Engineer."
        )
        mock_analyze.return_value = ParsedJobData(
            title="Senior Cybersecurity Engineer",
            company_name="Lululemon",
            description="**[CLOSED — applications no longer accepted]** "
                        "About the job. We are hiring.",
            closed_evidence="role is closed and no longer accepting applications",
        )
        scrape = Scrape.objects.create(
            url="https://www.linkedin.com/jobs/view/4383047961/",
            status="completed",
            job_content=active_page,
            source="scrape",
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        job = JobPost.objects.get(pk=scrape.job_post_id)
        # Evidence quote was NOT in source → posting_status must remain None
        self.assertIsNone(job.posting_status)
        # Synthetic [CLOSED ...] prefix on the LLM description must be
        # stripped before persistence
        self.assertFalse(
            job.description.lstrip().lower().startswith("[closed"),
            f"Banner survived strip: {job.description[:80]!r}",
        )
        self.assertFalse(
            job.description.lstrip().startswith("**"),
            f"Markdown-bold prefix not stripped: {job.description[:80]!r}",
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_substantiated_closed_evidence_marks_post_closed(self, mock_analyze):
        # Source page does carry a closed phrase verbatim.
        closed_page = (
            "Senior Engineer. About this role. We are no longer accepting "
            "applications for this position. Thanks for your interest."
        )
        evidence = "no longer accepting applications for this position"
        mock_analyze.return_value = ParsedJobData(
            title="Senior Engineer",
            company_name="Lululemon",
            description="Senior Engineer. About this role. Thanks for your interest.",
            closed_evidence=evidence,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/closed-role",
            status="completed",
            job_content=closed_page,
            source="scrape",
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        job = JobPost.objects.get(pk=scrape.job_post_id)
        # Evidence found in source → posting_status correctly set
        self.assertEqual(job.posting_status, "closed")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_verbatim_quote_must_match_curated_closed_phrase(self, mock_analyze):
        """jp 1532 incident (2026-05-01): a degraded LinkedIn capture
        (756 chars of mostly nav chrome — "Promoted by hirer · Responses
        managed off LinkedIn", "Save", "Apply", footer links) flipped
        the post to closed because the substring guard alone accepted
        whatever short snippet the LLM emitted as the "closed quote".
        Two-gate fix: the quote must (a) appear verbatim AND (b) match
        a curated closed-state phrase.
        """
        # Realistic LinkedIn-chrome page text — no closed phrase anywhere.
        thin_chrome_page = (
            "0 notifications\nSkip to main content\nHome\nMy Network\n"
            "Jobs\nGitHub\n\nSoftware Engineer II, Security\n\n"
            "United States · Reposted 1 day ago · Over 100 people clicked apply\n\n"
            "Promoted by hirer · Responses managed off LinkedIn\n\n"
            "Remote\nFull-time\nApply\nSave\n"
        )
        # An LLM-emitted "quote" that IS verbatim in the page but does
        # NOT semantically express closed state. Pre-fix this would have
        # passed the substring check and silently flipped the post.
        bogus_evidence = "Promoted by hirer · Responses managed off LinkedIn"
        mock_analyze.return_value = ParsedJobData(
            title="Software Engineer II, Security",
            company_name="GitHub",
            description="Software Engineer II, Security at GitHub.",
            closed_evidence=bogus_evidence,
        )
        scrape = Scrape.objects.create(
            url="https://www.linkedin.com/jobs/view/4386478229/",
            status="completed",
            job_content=thin_chrome_page,
            source="scrape",
            created_by=self.user,
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        job = JobPost.objects.get(pk=scrape.job_post_id)
        # Quote was verbatim but not a curated closed phrase → must
        # remain None (post stays open).
        self.assertIsNone(job.posting_status)

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_graph_detected_status_wins_over_curated_scan(self, mock_analyze):
        """Channel priority: when the agents-side scrape-graph
        DetectClosedState node has already verdict'd the post (writing
        Scrape.detected_posting_status), the extractor must use that
        result directly rather than re-running detect_posting_status on
        raw_source. Reasons:
          - the graph's CSS / phrase / Haiku-validated channels are
            stricter than a global regex scan against the raw page text
            (which over-fires on UI chrome)
          - the graph already handled the LinkedIn-style thin-chrome
            captures that historically false-positived
        Ordering: graph (channel 1) > curated scan (channel 2) >
        LLM-emitted closed_evidence (channel 3).
        """
        # Page text DOES contain a curated closed phrase — channel 2 would
        # fire if it ran. But channel 1 says the graph saw the page open
        # (graph wrote None / empty), so the curated scan should still
        # run as fallback. (The graph not firing means it didn't detect
        # closed — which is silent, not "open".)
        # First case: graph fired closed → channel 1 wins, evidence
        # bypasses raw_source check.
        page_with_phrase = (
            "Senior Engineer. About the role. We are no longer accepting "
            "applications for this position. Thanks."
        )
        mock_analyze.return_value = ParsedJobData(
            title="Senior Engineer",
            company_name="Lululemon",
            description="Senior Engineer. About the role.",
            closed_evidence=None,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/graph-detected",
            status="completed",
            job_content=page_with_phrase,
            source="scrape",
            created_by=self.user,
            detected_posting_status="closed",
            detected_closed_evidence=".job-closed-banner",
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        job = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(job.posting_status, "closed")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_graph_silent_falls_through_to_curated_scan(self, mock_analyze):
        """When DetectClosedState is silent (None/empty), the channel-2
        curated raw-source scan still runs and can flip the post."""
        page_with_phrase = (
            "Senior Engineer. About the role. We are no longer accepting "
            "applications for this position. Thanks."
        )
        mock_analyze.return_value = ParsedJobData(
            title="Senior Engineer",
            company_name="Lululemon",
            description="Senior Engineer. About the role.",
            closed_evidence=None,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/graph-silent",
            status="completed",
            job_content=page_with_phrase,
            source="scrape",
            created_by=self.user,
            # detected_posting_status omitted → defaults to None
        )
        parse_scrape(scrape.id, user_id=self.user.id, sync=True)
        scrape.refresh_from_db()
        job = JobPost.objects.get(pk=scrape.job_post_id)
        # Channel 2 (curated scan) caught the phrase
        self.assertEqual(job.posting_status, "closed")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_strip_closed_banner_prefix_idempotent(self, mock_analyze):
        # No evidence, no source phrase — but the LLM still injected the
        # prefix. _strip_closed_banner_prefix must remove it.
        from job_hunting.lib.parsers.job_post_extractor import (
            _strip_closed_banner_prefix,
        )
        cases = [
            "**[CLOSED — applications no longer accepted]** About the job.",
            "[CLOSED] About the job.",
            "[CLOSED: applications no longer accepted]\n\nAbout the job.",
            "  **[Closed - applications no longer accepted]**\nAbout the job.",
            "About the job. [CLOSED] mid-text reference.",  # only prefix stripped
        ]
        expectations = [
            "About the job.",
            "About the job.",
            "About the job.",
            "About the job.",
            "About the job. [CLOSED] mid-text reference.",
        ]
        for raw, expected in zip(cases, expectations):
            self.assertEqual(
                _strip_closed_banner_prefix(raw).lstrip(), expected,
                f"Failed on input: {raw!r}",
            )


class TestSourceTrustRanking(TestCase):
    """The trust ladder that decides which source wins on collision."""

    def test_extension_outranks_email(self):
        self.assertGreater(source_trust("extension"), source_trust("email"))
        self.assertGreater(source_trust("extension"), source_trust("email_direct"))

    def test_paste_outranks_scrape_outranks_manual_outranks_email(self):
        self.assertGreater(source_trust("paste"), source_trust("scrape"))
        self.assertGreater(source_trust("scrape"), source_trust("manual"))
        self.assertGreater(source_trust("manual"), source_trust("email"))

    def test_email_and_email_direct_tie(self):
        self.assertEqual(source_trust("email"), source_trust("email_direct"))

    def test_unknown_source_treated_as_manual(self):
        self.assertEqual(source_trust("nonsense"), source_trust("manual"))
        self.assertEqual(source_trust(None), source_trust("manual"))


class TestTrustAwareOverwrite(TestCase):
    """Higher-trust source on collision overwrites all overwritable
    fields and writes a JobPostOverwriteDecision audit row.

    Symmetric with the ship message: the cc_auto self-heal path. An
    extension push that canonical-link-collides with a hallucinated
    email-pipeline post replaces the wrong title/company/description in
    place — no separate audit step, no manual triage required.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="trust", password="pass")
        self.email_company = Company.objects.create(name="Wrong Company LLC")
        self.real_company = Company.objects.create(name="Real Company Inc")
        self.parsed_real = ParsedJobData(
            title="Real Job Title",
            company_name="Real Company Inc",
            description="Real description from the actual page. " * 20,
            location="San Francisco, CA",
            remote=True,
        )

    def _make_existing_email_post(self, *, link, description, complete=True):
        return JobPost.objects.create(
            title="Hallucinated Email Title",
            company=self.email_company,
            link=link,
            description=description,
            location="Wrong Location",
            source="email",
            complete=complete,
            created_by=self.user,
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_extension_overwrites_email_jobpost_full_description(self, mock_analyze):
        mock_analyze.return_value = self.parsed_real
        link = "https://example.com/real-job/abc"
        existing = self._make_existing_email_post(
            link=link, description="hallucinated description " * 30,
        )
        scrape = Scrape.objects.create(
            url=link,
            status="completed",
            job_content="real page text " * 50,
            source="extension",
            created_by=self.user,
        )

        parse_scrape(scrape.id, user_id=self.user.id, sync=True)

        existing.refresh_from_db()
        self.assertEqual(existing.title, "Real Job Title")
        self.assertEqual(existing.company_id, self.real_company.id)
        self.assertEqual(existing.location, "San Francisco, CA")
        self.assertEqual(existing.remote, True)
        self.assertIn("Real description", existing.description)
        # The whole point: source flipped to the higher-trust new value.
        self.assertEqual(existing.source, "extension")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_extension_overwrites_writes_audit_row(self, mock_analyze):
        mock_analyze.return_value = self.parsed_real
        link = "https://example.com/audit-job/xyz"
        existing = self._make_existing_email_post(
            link=link, description="long existing description " * 25,
        )
        scrape = Scrape.objects.create(
            url=link,
            status="completed",
            job_content="real page text " * 50,
            source="extension",
            created_by=self.user,
        )

        parse_scrape(scrape.id, user_id=self.user.id, sync=True)

        decisions = JobPostOverwriteDecision.objects.filter(job_post=existing)
        self.assertEqual(decisions.count(), 1)
        d = decisions.first()
        self.assertEqual(d.previous_source, "email")
        self.assertEqual(d.new_source, "extension")
        self.assertEqual(d.triggering_scrape_id, scrape.id)
        self.assertEqual(d.created_by_id, self.user.id)
        # Diff captures the actual changes — title and source at minimum.
        self.assertIn("title", d.changed_fields)
        self.assertEqual(
            d.changed_fields["title"]["before"], "Hallucinated Email Title"
        )
        self.assertEqual(d.changed_fields["title"]["after"], "Real Job Title")
        self.assertIn("source", d.changed_fields)
        self.assertEqual(d.changed_fields["source"]["before"], "email")
        self.assertEqual(d.changed_fields["source"]["after"], "extension")

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_extension_overwrites_email_stub(self, mock_analyze):
        # Same flip applies to the stub-upgrade path (link hit, thin
        # existing description). Symmetry across both branches matters
        # because the cc_auto hallucination case can be either: jp 1724
        # had a thin description ("This remote position offers $50K-
        # $85K..."); other rows might have richer hallucinated bodies.
        mock_analyze.return_value = self.parsed_real
        link = "https://example.com/stub-job/123"
        existing = self._make_existing_email_post(
            link=link, description="", complete=False,
        )
        scrape = Scrape.objects.create(
            url=link,
            status="completed",
            job_content="real page text " * 50,
            source="extension",
            created_by=self.user,
        )

        parse_scrape(scrape.id, user_id=self.user.id, sync=True)

        existing.refresh_from_db()
        self.assertEqual(existing.source, "extension")
        self.assertEqual(existing.title, "Real Job Title")
        self.assertEqual(
            JobPostOverwriteDecision.objects.filter(job_post=existing).count(), 1
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_email_does_not_overwrite_extension_jobpost(self, mock_analyze):
        # Trust ranking is one-way: lower-trust never overwrites higher-
        # trust. Even though an email-sourced scrape lands on a canonical
        # link match, the extension-originated post is preserved.
        mock_analyze.return_value = self.parsed_real
        link = "https://example.com/extension-first/aa"
        existing = JobPost.objects.create(
            title="Extension-Authored Title",
            company=self.real_company,
            link=link,
            description="extension-authored description " * 30,
            source="extension",
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url=link,
            status="completed",
            job_content="email digest text " * 50,
            source="email",
            created_by=self.user,
        )

        parse_scrape(scrape.id, user_id=self.user.id, sync=True)

        existing.refresh_from_db()
        self.assertEqual(existing.source, "extension")
        self.assertEqual(existing.title, "Extension-Authored Title")
        self.assertFalse(
            JobPostOverwriteDecision.objects.filter(job_post=existing).exists()
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_same_trust_no_overwrite_no_audit(self, mock_analyze):
        # paste vs paste = same trust = legacy duplicate-merge behavior,
        # no overwrite, no audit row. Important so re-paste of the same
        # URL doesn't keep churning audit rows.
        mock_analyze.return_value = self.parsed_real
        link = "https://example.com/paste-vs-paste/q"
        existing = JobPost.objects.create(
            title="Original Title",
            company=self.email_company,
            link=link,
            description="original full description " * 30,
            source="paste",
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url=link,
            status="completed",
            job_content="re-paste text " * 50,
            source="paste",
            created_by=self.user,
        )

        parse_scrape(scrape.id, user_id=self.user.id, sync=True)

        existing.refresh_from_db()
        self.assertEqual(existing.source, "paste")
        self.assertEqual(existing.title, "Original Title")
        self.assertFalse(
            JobPostOverwriteDecision.objects.filter(job_post=existing).exists()
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_overwrite_via_fingerprint_match(self, mock_analyze):
        # Symmetric path: when the existing post is matched via
        # title+company fingerprint (no link match), the trust-aware
        # overwrite still fires. jp 1603 incident territory.
        # ParsedJobData.title and .company_name must equal the existing
        # post's title+company for the fingerprint branch to fire.
        mock_analyze.return_value = ParsedJobData(
            title="Common Title",
            company_name="Real Company Inc",
            description="Updated description from extension. " * 25,
            location="Updated Location",
        )
        existing = JobPost.objects.create(
            title="Common Title",
            company=self.real_company,
            link="https://other-host.example.com/different-link",
            description="email-stub description",
            source="email",
            created_by=self.user,
        )
        scrape = Scrape.objects.create(
            url="https://example.com/extension-saw-this",
            status="completed",
            job_content="extension page text " * 50,
            source="extension",
            created_by=self.user,
        )

        parse_scrape(scrape.id, user_id=self.user.id, sync=True)

        existing.refresh_from_db()
        self.assertEqual(existing.source, "extension")
        self.assertEqual(existing.location, "Updated Location")
        self.assertEqual(
            JobPostOverwriteDecision.objects.filter(job_post=existing).count(), 1
        )
