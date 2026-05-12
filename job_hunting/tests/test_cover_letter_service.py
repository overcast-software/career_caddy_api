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
