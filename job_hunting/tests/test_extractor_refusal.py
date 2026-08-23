"""The extractor must be allowed to say "I could not find this".

Regression suite for the incident behind scrape ``X04b4IjnTi`` → job
post ``rHeRo6qWCG`` (2026-08-15, a LinkedIn posting). All 19 description
selectors timed out; the capture was 808 characters of top card and site
footer, no job body at all. The model was then asked for a full job
posting from that.

Two things made invention the only legal answer:

1. ``_build_agent_for_model`` built ``Agent(model,
   output_type=ParsedJobData)`` with no system prompt, and
   ``analyze_with_ai``'s prompt was a bare field list. Nothing anywhere
   told the model not to invent.
2. ``ParsedJobData.title`` and ``.company_name`` were required
   ``str``/``min_length=1`` with validators that RAISED on empty, and
   there was no field in which to express failure. pydantic-ai feeds
   validation errors back to the model and retries — so an honest empty
   answer was rejected and the model was pushed until it produced prose.

The fix is both halves, and the schema half is the load-bearing one: a
prompt asks, a schema guarantees. The tests below are grouped by the
property each defends, and every one of them fails against the
pre-fix code.
"""
from unittest.mock import patch

import pydantic
from django.contrib.auth import get_user_model
from django.test import TestCase

from job_hunting.api.views.admin import _agent_role_specs
from job_hunting.lib.parsers.completeness_reviewer import (
    CompletenessReviewer,
    ReviewDecision,
    is_not_captured_sentinel,
    maybe_review_and_persist,
)
from job_hunting.lib.parsers.job_post_extractor import (
    _EXTRACTOR_SYSTEM_PROMPT,
    JobPostExtractor,
    ParsedJobData,
)
from job_hunting.models import Company, JobPost, Scrape

User = get_user_model()


# The actual shape of the rHeRo6qWCG capture: LinkedIn top card plus
# site footer, ~800 characters, not one word of job description. Kept
# verbatim-ish rather than reduced to "chrome" because the whole point
# is that this looks *almost* like a posting — it has a title-ish
# string and a company-ish string in it, which is exactly what tempts a
# model into filling in the rest.
CHROME_ONLY_CAPTURE = (
    "Skip to main content LinkedIn Join now Sign in\n"
    "Software Engineer\n"
    "Bellevue, WA · Reposted 2 weeks ago · Over 100 people clicked apply\n"
    "Use AI to assess how you fit\n"
    "Save Save Software Engineer Show more options\n"
    "Am I a good fit for this job? How can I best position myself for this job? "
    "Tell me more about the company culture\n"
    "Seniority level Mid-Senior level Employment type Full-time\n"
    "Referrals increase your chances of interviewing by 2x\n"
    "See who you know Get notified about new Software Engineer jobs\n"
    "Sign in to set job alerts for Software Engineer roles.\n"
    "Similar jobs People also viewed Explore collaborative articles\n"
    "We're unlocking community knowledge in a new way. Experts add insights "
    "directly into each article, started with the help of AI.\n"
    "Explore More Looking for talent? Post a job\n"
    "LinkedIn Corporation 2026 About Accessibility User Agreement Privacy "
    "Policy Cookie Policy Copyright Policy Brand Policy Guest Controls "
    "Community Guidelines Language\n"
)


class TestRefusalIsExpressible(TestCase):
    """The schema half: an honest "nothing here" must validate.

    This is the whole bug in one assertion. Before the fix, the only
    output ``ParsedJobData`` would accept carried a title and a company
    name — so on a page that had neither, the model's choice was to
    invent them or to be retried until it did.
    """

    def test_refusal_validates(self):
        parsed = ParsedJobData(
            extraction_failed=True,
            extraction_failure_reason=(
                "Page rendered only the LinkedIn top card; no description "
                "subtree present."
            ),
        )
        self.assertTrue(parsed.extraction_failed)
        self.assertIsNone(parsed.title)
        self.assertIsNone(parsed.company_name)

    def test_refusal_is_cheaper_than_an_extraction(self):
        """A refusal costs two fields. An extraction costs two required
        identity fields plus whatever prose the model invents to fill
        the rest. If a refusal ever costs more than an invention, the
        model is being paid to lie and this test should catch it."""
        refusal = ParsedJobData(
            extraction_failed=True, extraction_failure_reason="No posting."
        )
        populated = {
            k: v for k, v in refusal.model_dump().items() if v not in (None, False)
        }
        self.assertEqual(
            set(populated), {"extraction_failed", "extraction_failure_reason"}
        )

    def test_refusal_cannot_smuggle_a_guessed_identity(self):
        """Hedging is not a third option. A model that flags failure AND
        supplies a title would leave process_evaluation's caller to
        decide which half to believe."""
        parsed = ParsedJobData(
            title="Software Engineer",
            company_name="LinkedIn",
            extraction_failed=True,
            extraction_failure_reason="Only the top card rendered.",
        )
        self.assertIsNone(parsed.title)
        self.assertIsNone(parsed.company_name)

    def test_refusal_without_a_reason_gets_one(self):
        parsed = ParsedJobData(extraction_failed=True)
        self.assertTrue(parsed.extraction_failure_reason)

    def test_blank_identity_normalizes_to_absent(self):
        """Blank used to raise from the field validator, which is what
        produced the retry loop. It now means "absent" and the
        model-level check decides — so a model that answers with empty
        strings plus the failure flag is honest, not malformed."""
        parsed = ParsedJobData(
            title="   ",
            company_name="",
            extraction_failed=True,
            extraction_failure_reason="Nothing on the page but navigation.",
        )
        self.assertIsNone(parsed.title)
        self.assertIsNone(parsed.company_name)

    def test_missing_identity_without_the_flag_still_raises(self):
        """The guarantee downstream consumers had is preserved: a
        *successful* extraction still carries title + company."""
        with self.assertRaises(pydantic.ValidationError) as ctx:
            ParsedJobData(description="Some prose but no identity.")
        self.assertIn("extraction_failed", str(ctx.exception))

    def test_retry_message_names_the_refusal_path(self):
        """pydantic-ai hands a validation error back to the model as a
        retry prompt. That retry used to push toward inventing a title;
        it must now point at the legal alternative instead."""
        with self.assertRaises(pydantic.ValidationError) as ctx:
            ParsedJobData()
        message = str(ctx.exception)
        self.assertIn("extraction_failed=true", message)
        self.assertIn("instead of guessing", message)

    def test_ordinary_extraction_is_unchanged(self):
        parsed = ParsedJobData(
            title="  Senior Engineer  ",
            company_name="  Acme Corp  ",
            description="Real responsibilities and qualifications.",
        )
        self.assertEqual(parsed.title, "Senior Engineer")
        self.assertEqual(parsed.company_name, "Acme Corp")
        self.assertFalse(parsed.extraction_failed)
        self.assertIsNone(parsed.extraction_failure_reason)


class TestAntiInventionPrompt(TestCase):
    """The prompt half: the model has to be told the refusal exists.

    The extractor was the only prose-producing agent in the codebase
    with no system prompt at all — ``completeness_reviewer``,
    ``description_arbiter``, ``job_matcher`` and ``AnswerService`` all
    have one.
    """

    def test_agent_is_built_with_the_system_prompt(self):
        """Assert the prompt wiring WITHOUT standing up a live SDK client.

        ``_build_agent_for_model`` constructs the provider model before it
        constructs the Agent, and ``OpenAIResponsesModel(...)`` builds a real
        ``AsyncOpenAI``, which raises ``OpenAIError: The api_key client option
        must be set`` when OPENAI_API_KEY is absent. Patching ``Agent`` alone
        therefore passes on a developer machine that has the key exported and
        fails in CI, which is exactly what happened on this PR's first run.
        This test only cares which kwargs reach ``Agent``, so the model
        construction is patched out too and no key is ever needed.
        """
        captured = {}

        class _FakeAgent:
            def __init__(self, model, **kwargs):
                captured.update(kwargs)

        with (
            patch("job_hunting.lib.parsers.job_post_extractor.Agent", _FakeAgent),
            patch(
                "job_hunting.lib.parsers.job_post_extractor.OpenAIResponsesModel"
            ) as fake_model,
        ):
            JobPostExtractor()._build_agent_for_model("openai:gpt-4o-mini")

        # The provider dispatch still happened — the bare name reached the
        # OpenAI model class rather than another provider's.
        fake_model.assert_called_once_with("gpt-4o-mini")
        self.assertEqual(captured.get("system_prompt"), _EXTRACTOR_SYSTEM_PROMPT)

    def test_system_prompt_forbids_invention_and_blesses_refusal(self):
        # Collapsed because the constant is hard-wrapped — an assertion
        # that breaks on a reflow teaches nothing.
        prompt = " ".join(_EXTRACTOR_SYSTEM_PROMPT.lower().split())
        self.assertIn("never invent", prompt)
        self.assertIn("extraction_failed=true", prompt)
        self.assertIn("correct and expected outcome", prompt)

    def test_user_prompt_offers_the_refusal_too(self):
        """The system prompt states the policy; the per-call prompt has
        to restate the mechanism, because a Tier1/2/3 escalation builds
        a fresh agent and the field list is what the model reads last."""
        user = User.objects.create_user(username="prompt", password="pw")
        scrape = Scrape.objects.create(
            url="https://www.linkedin.com/jobs/view/4453904340",
            status="completed",
            job_content=CHROME_ONLY_CAPTURE,
            created_by=user,
        )
        captured = {}

        class _FakeResult:
            output = ParsedJobData(
                extraction_failed=True, extraction_failure_reason="chrome only"
            )

            def usage(self):
                return None

        class _FakeAgent:
            def run_sync(self, prompt):
                captured["prompt"] = prompt
                return _FakeResult()

        extractor = JobPostExtractor()
        extractor.agent = _FakeAgent()
        with patch.object(JobPostExtractor, "_record_usage", lambda *a, **k: None):
            extractor.analyze_with_ai(scrape)

        self.assertIn("extraction_failed=true", captured["prompt"])
        self.assertIn("extraction_failure_reason", captured["prompt"])


class TestChromeOnlyCaptureIsRefused(TestCase):
    """End-to-end: the test that matters.

    Given a page of pure chrome and a model that answers honestly, no
    JobPost is minted, the scrape lands in ``failed``, and the operator
    sees the model's own reason rather than a guessed sentinel word.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="chrome", password="pw")
        self.scrape = Scrape.objects.create(
            url="https://www.linkedin.com/jobs/view/4453904340",
            status="extracting",
            job_content=CHROME_ONLY_CAPTURE,
            created_by=self.user,
        )

    def test_no_job_post_is_minted_from_chrome(self):
        self.assertLess(len(CHROME_ONLY_CAPTURE), 1000, "capture must stay thin")

        refusal = ParsedJobData(
            extraction_failed=True,
            extraction_failure_reason=(
                "Page rendered only the LinkedIn top card and site footer; "
                "no job description present."
            ),
        )
        extractor = JobPostExtractor()
        with patch.object(JobPostExtractor, "analyze_with_ai", return_value=refusal):
            ok = extractor.parse(self.scrape, user=self.user)

        self.assertFalse(ok)
        self.assertEqual(extractor.last_outcome, "extraction_failed")
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.status, "failed")
        self.assertIsNone(self.scrape.job_post_id)
        self.assertIn("top card", self.scrape.failure_reason)
        self.assertEqual(JobPost.objects.count(), 0)
        self.assertEqual(Company.objects.count(), 0, "a refusal must not mint a Company")

    def test_refusal_outcome_is_not_reviewable(self):
        """There is no JobPost to review, so the completeness gate must
        not be asked to judge one."""
        from job_hunting.lib.parsers.completeness_reviewer import _REVIEWABLE_OUTCOMES

        self.assertNotIn("extraction_failed", _REVIEWABLE_OUTCOMES)


class TestNotCapturedSentinelSurvivesPersistence(TestCase):
    """A refusal is only as good as its survival through persistence.

    The partial-render case is the other half of the same incident: the
    top card *does* carry a title and a company, so the honest answer is
    an extraction whose description is the labelled
    ``[DESCRIPTION NOT CAPTURED — …]`` marker that the linkedin.com
    ScrapeProfile hint (migration 0101) instructs, and that the agents/
    scrape-graph now recognizes.

    That marker used to sail straight through: it is ~120 non-empty
    characters, so the cheap empty-description gate missed it, and the
    reviewer's own prompt has never heard of the sentinel and is told to
    "default to ACCEPT when uncertain". Live evidence that this is not
    hypothetical: jp rHeRo6qWCG carries exactly this description and
    still has ``complete=True``.
    """

    SENTINEL = (
        "[DESCRIPTION NOT CAPTURED — LinkedIn page rendered only the top "
        "card; rescrape later or capture via the cc_sender extension]"
    )

    def setUp(self):
        self.user = User.objects.create_user(username="sentinel", password="pw")
        self.company = Company.objects.create(name="Acme Corp")

    def _make_jp(self, description):
        return JobPost.objects.create(
            title="Software Engineer",
            company=self.company,
            description=description,
            link="https://www.linkedin.com/jobs/view/4453904340",
            complete=True,
            created_by=self.user,
        )

    @patch.object(CompletenessReviewer, "review")
    def test_sentinel_description_flips_complete_without_paying_for_an_llm(
        self, mock_review
    ):
        # Mirrors the real prompt's "default to ACCEPT when uncertain":
        # if the sentinel reaches the LLM at all, the post stays complete.
        mock_review.return_value = ReviewDecision(
            looks_like_job_description=True,
            confidence="low",
            reasoning="Uncertain; defaulting to accept.",
        )
        jp = self._make_jp(self.SENTINEL)
        decision = maybe_review_and_persist(jp, last_outcome="created")

        mock_review.assert_not_called()
        self.assertFalse(decision.looks_like_job_description)
        jp.refresh_from_db()
        self.assertFalse(jp.complete)

    @patch.object(CompletenessReviewer, "review")
    def test_real_description_still_reaches_the_llm(self, mock_review):
        mock_review.return_value = ReviewDecision(
            looks_like_job_description=True,
            confidence="high",
            reasoning="Real posting.",
        )
        jp = self._make_jp(
            "You will own our billing pipeline end to end. Requirements: "
            "five years of Python, strong Postgres, and Kafka experience."
        )
        maybe_review_and_persist(jp, last_outcome="created")

        mock_review.assert_called_once()
        jp.refresh_from_db()
        self.assertTrue(jp.complete)

    def test_matches_both_stub_wordings_in_circulation(self):
        """Two producers emit this marker with different prose, and the
        api gate has to recognize both or the one it misses reaches an
        LLM that defaults to ACCEPT.

        - migration 0101's linkedin.com ``extraction_hints`` (the LLM
          writes it because a per-host hint told it to).
        - the agents/ scrape-graph's own ``_STUB_DESCRIPTION_TEMPLATE``
          (deterministic, written when escalation is exhausted).

        If either side reworks its wording, this test is where it
        surfaces — the alternative is a silent gap.
        """
        graph_stub = (
            "[DESCRIPTION NOT CAPTURED — the scrape reached this posting "
            "but could not read its description (the page returned no "
            "description body). Re-scrape the link, or send the page from "
            "the browser extension while it is open.]"
        )
        self.assertTrue(is_not_captured_sentinel(graph_stub))
        self.assertTrue(is_not_captured_sentinel(self.SENTINEL))

    def test_sentinel_matcher_is_anchored(self):
        """A real description that quotes the marker mid-body is a real
        description. Over-eager matching here would silently mark good
        posts incomplete."""
        self.assertTrue(is_not_captured_sentinel(self.SENTINEL))
        self.assertTrue(is_not_captured_sentinel("  [not captured]  "))
        self.assertFalse(is_not_captured_sentinel(""))
        self.assertFalse(is_not_captured_sentinel("Real prose about the role."))
        self.assertFalse(
            is_not_captured_sentinel(
                "We use a tool that emits [DESCRIPTION NOT CAPTURED] on "
                "failure, and you will own it. Requirements: Python."
            )
        )


class TestAgentRoleRegistry(TestCase):
    """``api/CLAUDE.md``: an AI-backed feature without a registry entry
    is invisible and unchangeable. ``completeness_reviewer`` read
    ``COMPLETENESS_REVIEWER_MODEL`` but was absent — the same gap that
    once hid ``answer``, ``cover_letter`` and ``job_matcher``, and it
    was hiding the one role that can veto a fabricated post."""

    def test_completeness_reviewer_is_registered(self):
        roles = {spec[0]: spec for spec in _agent_role_specs()}
        self.assertIn("completeness_reviewer", roles)
        _, _, env_var, default = roles["completeness_reviewer"]
        self.assertEqual(env_var, "COMPLETENESS_REVIEWER_MODEL")
        self.assertEqual(default, "anthropic:claude-haiku-4-5")

    def test_registered_default_matches_the_module(self):
        """A registry row that disagrees with the code is worse than no
        row — it reports a model that is not in use."""
        from job_hunting.lib.parsers import completeness_reviewer

        roles = {spec[0]: spec for spec in _agent_role_specs()}
        self.assertEqual(roles["completeness_reviewer"][3], completeness_reviewer._DEFAULT_MODEL)
