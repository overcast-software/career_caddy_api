"""CC-202 — summary generation enqueue-contract (bucket-2).

The AI-generation branch of ``SummaryViewSet.create`` creates a pending
``Summary`` row and dispatches the work through the unified async producer
``enqueue('summary', **payload)`` (CC-214 pattern — the generic transport
switch picks Cloud Tasks on GCP or a ``Job`` row on self-host). These tests
assert the enqueue SEAM: ``enqueue`` is called with ``kind='summary'`` and the
NanoID-string ``summary_id`` payload. The ``summary_job`` worker leg itself is
covered by the worker tests; here we patch the seam so the real LLM never runs.

NOTE (blocker flagged on CC-202): ``Summary.job_post_id`` is a legacy
``IntegerField`` that was NOT migrated to a NanoID FK when JobPost swapped to a
string PK (CC-57). So the pending-``Summary`` write in the view rejects a real
NanoID JobPost id end-to-end — a pre-existing straggler independent of this
enqueue migration. To keep this a focused bucket-2 seam test (not gated on the
unrelated schema bug) we stub the row creation and assert only the enqueue
contract. The FK fix belongs to a separate schema ticket.
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

    def test_ai_path_enqueues_summary_kind_with_nanoid_payload(self):
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
        # A stub pending Summary carrying a NanoID string id, so the seam
        # assertion is not gated on the unrelated Summary.job_post_id
        # IntegerField straggler (blocker flagged on CC-202).
        stub = Summary(id="Smry000001", user_id=self.user.id, status="pending")
        with patch(
            "job_hunting.api.views.summaries.get_client", return_value=MagicMock()
        ), patch(
            "job_hunting.api.views.summaries.ApplicationPromptBuilder"
        ) as mock_builder, patch(
            "job_hunting.api.views.summaries.Summary.objects.create",
            return_value=stub,
        ), patch(
            "job_hunting.api.views.summaries.enqueue"
        ) as mock_enqueue:
            mock_builder.return_value.build_from_career_data.return_value = "resume md"
            resp = self.client.post(
                SUMMARIES_URL,
                data=payload,
                content_type="application/vnd.api+json",
            )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)

        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "summary")
        self.assertEqual(kwargs["summary_id"], stub.id)
        self.assertIsInstance(kwargs["summary_id"], str)
        # No explicit resume relationship => career-data path (resume_id None).
        self.assertIsNone(kwargs["resume_id"])
        self.assertIn("injected_prompt", kwargs)

    def test_manual_content_completes_without_enqueue(self):
        # Manual content is synchronous + terminal — never enqueues.
        stub = Summary(id="Smry000002", user_id=self.user.id, status="completed")
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
        with patch(
            "job_hunting.api.views.summaries.Summary.objects.create",
            return_value=stub,
        ), patch("job_hunting.api.views.summaries.enqueue") as mock_enqueue:
            resp = self.client.post(
                SUMMARIES_URL,
                data=payload,
                content_type="application/vnd.api+json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        mock_enqueue.assert_not_called()
