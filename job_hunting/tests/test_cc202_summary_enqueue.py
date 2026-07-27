"""CC-202 — summary generation enqueue-contract (bucket-2) + CC-218 FK end-to-end.

The AI-generation branch of ``SummaryViewSet.create`` creates a pending
``Summary`` row and dispatches the work through the unified async producer
``enqueue('summary', **payload)`` (CC-214 pattern — the generic transport
switch picks Cloud Tasks on GCP or a ``Job`` row on self-host). These tests
assert the enqueue SEAM: ``enqueue`` is called with ``kind='summary'`` and the
``summary_id`` payload. The ``summary_job`` worker leg itself is covered by the
worker tests; here we patch the seam so the real LLM never runs.

CC-218: ``Summary.job_post`` is now a real NanoID ``ForeignKey(JobPost)`` (was a
legacy ``IntegerField`` that rejected real NanoID JobPost ids end-to-end). These
tests now persist a REAL ``Summary`` against a REAL NanoID ``JobPost`` — the
exact write that failed before CC-218 — instead of stubbing ``objects.create``.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, Summary

User = get_user_model()
SUMMARIES_URL = "/api/v1/summaries/"


class TestSummaryEnqueueContract(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="summ", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.jp = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            created_by=self.user,
            description="a " * 100,
        )

    def test_ai_path_persists_summary_on_nanoid_jobpost_and_enqueues(self):
        payload = {
            "data": {
                "type": "summary",
                "attributes": {},
                "relationships": {
                    "job-post": {
                        "data": {"type": "job-post", "id": str(self.jp.id)}
                    }
                },
            }
        }
        with patch(
            "job_hunting.api.views.summaries.get_client", return_value=MagicMock()
        ), patch(
            "job_hunting.api.views.summaries.ApplicationPromptBuilder"
        ) as mock_builder, patch(
            "job_hunting.api.views.summaries.enqueue"
        ) as mock_enqueue:
            mock_builder.return_value.build_from_career_data.return_value = "resume md"
            resp = self.client.post(
                SUMMARIES_URL,
                data=payload,
                content_type="application/vnd.api+json",
            )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)

        # CC-218: the real pending Summary persisted, linked to the NanoID
        # JobPost via the FK — the write that failed before the FK swap.
        summary = Summary.objects.get(user_id=self.user.id, job_post=self.jp)
        self.assertEqual(summary.status, "pending")
        self.assertEqual(summary.job_post_id, self.jp.id)  # NanoID string FK
        self.assertIsInstance(summary.job_post_id, str)
        self.assertEqual(summary.job_post.title, "Engineer")  # FK traversal

        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "summary")
        self.assertEqual(kwargs["summary_id"], summary.id)
        # No explicit resume relationship => career-data path (resume_id None).
        self.assertIsNone(kwargs["resume_id"])
        self.assertIn("injected_prompt", kwargs)

    def test_manual_content_persists_completed_summary_on_nanoid_jobpost(self):
        # Manual content is synchronous + terminal — never enqueues, and the
        # completed Summary links to the NanoID JobPost (CC-218).
        payload = {
            "data": {
                "type": "summary",
                "attributes": {"content": "hand-written summary"},
                "relationships": {
                    "job-post": {
                        "data": {"type": "job-post", "id": str(self.jp.id)}
                    }
                },
            }
        }
        with patch("job_hunting.api.views.summaries.enqueue") as mock_enqueue:
            resp = self.client.post(
                SUMMARIES_URL,
                data=payload,
                content_type="application/vnd.api+json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        mock_enqueue.assert_not_called()

        summary = Summary.objects.get(user_id=self.user.id, job_post=self.jp)
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.content, "hand-written summary")
        self.assertEqual(summary.job_post_id, self.jp.id)


class TestSummaryNanoidFkMigration(TestCase):
    """CC-218: the Summary.job_post FK round-trips a real NanoID JobPost — the
    exact schema-level write that the legacy IntegerField rejected."""

    def setUp(self):
        self.user = User.objects.create_user(username="fk", password="pw")
        self.company = Company.objects.create(name="FK Co")
        self.jp = JobPost.objects.create(
            title="Backend", company=self.company, created_by=self.user,
            description="x " * 60,
        )

    def test_summary_create_with_nanoid_job_post_round_trips(self):
        # NanoID JobPost ids are 10-char strings; the old IntegerField column
        # could not store them (ValueError / DataError). The FK does.
        self.assertIsInstance(self.jp.id, str)
        s = Summary.objects.create(
            job_post=self.jp, user=self.user, content="c", status="completed"
        )
        s.refresh_from_db()
        self.assertEqual(s.job_post_id, self.jp.id)
        self.assertEqual(s.job_post.title, "Backend")
        # Reverse relation.
        self.assertIn(s, self.jp.summaries.all())

    def test_summary_create_with_job_post_id_kwarg_round_trips(self):
        # The write sites pass job_post_id=<nanoid> — must still work.
        s = Summary.objects.create(
            job_post_id=self.jp.id, user=self.user, status="pending"
        )
        s.refresh_from_db()
        self.assertEqual(s.job_post_id, self.jp.id)
