from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.lib.services.application_prompt_builder import (
    ApplicationPromptBuilder,
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
