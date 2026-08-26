import os
from unittest import mock

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.lib.services.application_prompt_builder import (
    MAX_SECTION_CHARS_DEFAULT,
    MAX_SECTION_CHARS_ENV,
    ApplicationPromptBuilder,
    resolve_max_section_chars,
)
from job_hunting.models import Question

User = get_user_model()


class TestInjectedPromptOrdering(TestCase):
    """The user-supplied injected_prompt must precede the default preamble.

    Regression: the old layout put it last under '## Additional Instructions',
    and the strong leading 'OUTPUT FORMAT — strictly plain text' directive
    beat it on every LLM we tried — repro was 'write every word backwards'
    being silently ignored on /job-posts/<id>/questions/<id>/answers/new.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="prompt_user", password="pw")
        self.question = Question.objects.create(
            content="What's your experience with Python?",
            created_by=self.user,
        )
        self.context = {
            "question": self.question,
            "job_post": None,
            "company": None,
            "resumes": [],
            "resume": None,
            "cover_letters": [],
            "qas": [],
            "user": self.user,
        }

    def test_injected_prompt_appears_before_default_preamble(self):
        builder = ApplicationPromptBuilder()
        prompt = builder.build(
            self.context,
            injected_prompt="write every word backwards",
        )

        injected_idx = prompt.find("write every word backwards")
        default_idx = prompt.find("Answer ONLY the question")
        self.assertNotEqual(injected_idx, -1, "injected prompt missing from output")
        self.assertNotEqual(default_idx, -1, "default preamble missing from output")
        self.assertLess(
            injected_idx,
            default_idx,
            "injected prompt must precede default preamble so the model treats it as the controlling directive",
        )

    def test_injected_prompt_flagged_as_priority_override(self):
        builder = ApplicationPromptBuilder()
        prompt = builder.build(
            self.context,
            injected_prompt="be enthusiastic",
        )
        self.assertIn("PRIORITY", prompt)
        self.assertIn("User Instructions", prompt)

    def test_no_injected_prompt_omits_override_section(self):
        builder = ApplicationPromptBuilder()
        prompt = builder.build(self.context)
        self.assertNotIn("User Instructions (PRIORITY", prompt)
        self.assertIn("Answer ONLY the question", prompt)

    def test_injected_prompt_is_also_restated_last(self):
        """Leading position was not enough — see the ticket.

        Doug, 2026-08-25: "I gave it a clear directive to highlight mcp and it
        didn't." The PRIORITY block is first, but everything between it and
        the end — career profile, job description, resumes, cover letters, Q&A
        history — can run to tens of thousands of characters, and the
        directive was 27. Repeating it as the LAST thing read is the cheapest
        lever available, so the test asserts BOTH ends, not just presence.
        """
        builder = ApplicationPromptBuilder()
        prompt = builder.build(
            self.context,
            injected_prompt="mention careercaddy mcp",
        )

        first = prompt.find("mention careercaddy mcp")
        last = prompt.rfind("mention careercaddy mcp")
        self.assertNotEqual(first, -1, "injected prompt missing entirely")
        self.assertNotEqual(
            first, last, "injected prompt appears once — it must also be restated at the end"
        )
        self.assertIn("controlling directive", prompt)
        # Nothing may follow the restatement. If a new section is appended to
        # build() below it, this fails — which is the point: the reminder is
        # only worth anything while it is genuinely last.
        self.assertTrue(
            prompt.rstrip().endswith("mention careercaddy mcp"),
            "the restatement must be the final thing in the prompt",
        )

    def test_no_injected_prompt_means_no_trailing_reminder(self):
        """An empty restatement would tell the model its last word is blank."""
        builder = ApplicationPromptBuilder()
        prompt = builder.build(self.context)
        self.assertNotIn("controlling directive", prompt)


class TestMaxSectionChars(TestCase):
    """The per-section cap is env-tunable, and unsafe values fall back.

    Raised from a hardcoded 60000 (Doug, 2026-08-25) now that cost is no
    longer the binding constraint. Made resolvable at runtime rather than
    re-hardcoded, so the next adjustment is a restart and not a deploy.
    """

    def test_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MAX_SECTION_CHARS_ENV, None)
            self.assertEqual(resolve_max_section_chars(), MAX_SECTION_CHARS_DEFAULT)

    def test_env_override_is_honoured(self):
        with mock.patch.dict(os.environ, {MAX_SECTION_CHARS_ENV: "250000"}):
            self.assertEqual(resolve_max_section_chars(), 250000)
            self.assertEqual(ApplicationPromptBuilder().max_section_chars, 250000)

    def test_garbage_and_nonpositive_values_fall_back(self):
        # A typo in an env var must not take answer generation down, and the
        # default is always a safe answer.
        for bad in ("banana", "", "0", "-1"):
            with mock.patch.dict(os.environ, {MAX_SECTION_CHARS_ENV: bad}):
                self.assertEqual(
                    resolve_max_section_chars(),
                    MAX_SECTION_CHARS_DEFAULT,
                    f"{bad!r} should fall back to the default",
                )

    def test_explicit_argument_still_wins(self):
        # The nine call sites that were NOT changed pass 60000 explicitly and
        # must keep getting exactly that.
        with mock.patch.dict(os.environ, {MAX_SECTION_CHARS_ENV: "250000"}):
            self.assertEqual(
                ApplicationPromptBuilder(max_section_chars=60000).max_section_chars,
                60000,
            )
