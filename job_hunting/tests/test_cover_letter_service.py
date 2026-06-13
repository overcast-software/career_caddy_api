from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.lib.services.cover_letter_service import CoverLetterService
from job_hunting.models import Company, CoverLetter, JobPost, Resume

User = get_user_model()


class TestCoverLetterServiceReturnShape(TestCase):
    """Regression: ``generate_cover_letter`` used to do its own
    ``CoverLetter.objects.get_or_create()`` inside the service. The POST
    view already creates a pending row before dispatching the worker
    thread, so the service's get_or_create produced a SECOND row whenever
    the generated content didn't match an existing empty one — which is
    every fresh generation. The fix returns the content string only; the
    view updates its pending row.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="cl_user", password="pw")
        self.company = Company.objects.create(name="Acme")
        self.job_post = JobPost.objects.create(
            title="Backend Engineer",
            description="Build things.",
            company=self.company,
            created_by=self.user,
        )
        self.resume = Resume.objects.create(
            user=self.user,
            name="Primary",
            title="Backend Engineer",
        )

        # Mock the OpenAI client so we never hit the network.
        self.ai_client = MagicMock()
        self.ai_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Dear hiring manager, ... Best, candidate.\n"
                    )
                )
            ]
        )

    def test_returns_content_string(self):
        svc = CoverLetterService(
            self.ai_client,
            self.job_post,
            resume=self.resume,
            resume_markdown="# resume\n\nfine.",
            user_id=self.user.id,
        )
        result = svc.generate_cover_letter()
        self.assertIsInstance(result, str)
        self.assertIn("Dear hiring manager", result)
        # Trailing whitespace stripped.
        self.assertEqual(result, result.strip())

    def test_does_not_create_cover_letter_row(self):
        # The view owns persistence — the service must not touch the DB.
        starting_count = CoverLetter.objects.count()
        svc = CoverLetterService(
            self.ai_client,
            self.job_post,
            resume=self.resume,
            resume_markdown="# resume\n\nfine.",
            user_id=self.user.id,
        )
        svc.generate_cover_letter()
        self.assertEqual(CoverLetter.objects.count(), starting_count)


class TestCoverLetterPromptOrdering(TestCase):
    """The user-supplied injected_prompt must precede the leading constraints.

    Regression: the old template put the injected_prompt under a trailing
    ``Additional Instructions:`` block, AFTER hard constraints like
    ``Output plain text only`` and ``Do not use the words moreover...``.
    Strong leading directives beat trailing ones — repro was Doug
    injecting custom instructions on /cover-letters/new and the model
    silently ignoring them. The answer-prompt path
    (``application_prompt_builder.py``) lifts the same block to the top
    under ``## User Instructions (PRIORITY ...)``; this mirrors that.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="cl_order_user", password="pw")
        self.company = Company.objects.create(name="Acme")
        self.job_post = JobPost.objects.create(
            title="Backend Engineer",
            description="Build things.",
            company=self.company,
            created_by=self.user,
        )
        self.resume = Resume.objects.create(
            user=self.user,
            name="Primary",
            title="Backend Engineer",
        )

        self.ai_client = MagicMock()
        self.ai_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="letter body\n")
                )
            ]
        )

    def _rendered_user_message(self, injected_prompt=None):
        svc = CoverLetterService(
            self.ai_client,
            self.job_post,
            resume=self.resume,
            resume_markdown="# resume\n\nfine.",
            user_id=self.user.id,
        )
        svc.generate_cover_letter(injected_prompt=injected_prompt)
        kwargs = self.ai_client.chat.completions.create.call_args.kwargs
        messages = kwargs["messages"]
        # messages[0] = system, messages[1] = user (the rendered template)
        return messages[1]["content"]

    def test_injected_prompt_appears_before_constraints(self):
        prompt = self._rendered_user_message(
            injected_prompt="write every word backwards"
        )
        injected_idx = prompt.find("write every word backwards")
        constraints_idx = prompt.find("Constraints:")
        self.assertNotEqual(injected_idx, -1, "injected prompt missing from rendered template")
        self.assertNotEqual(constraints_idx, -1, "Constraints block missing from rendered template")
        self.assertLess(
            injected_idx,
            constraints_idx,
            "injected prompt must precede the leading constraints so the model treats it as the controlling directive",
        )

    def test_injected_prompt_flagged_as_priority_override(self):
        prompt = self._rendered_user_message(injected_prompt="be enthusiastic")
        self.assertIn("## User Instructions (PRIORITY", prompt)

    def test_no_injected_prompt_omits_override_section(self):
        prompt = self._rendered_user_message(injected_prompt=None)
        self.assertNotIn("User Instructions (PRIORITY", prompt)
        self.assertIn("Constraints:", prompt)
