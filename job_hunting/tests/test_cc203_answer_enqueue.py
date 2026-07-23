"""CC-203 — question/answer generation enqueue-contract (bucket-2).

The AI-assist branch of ``AnswerViewSet.create`` creates a pending ``Answer``
row and dispatches generation through the unified async producer
``enqueue('answer', **payload)`` (CC-214 pattern). These tests assert the
enqueue SEAM: ``enqueue`` is called with ``kind='answer'`` and the
NanoID-string ``answer_id`` payload. The ``answer_job`` worker leg is covered
by the worker tests; here we patch the seam so the real LLM never runs.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.models import Answer, Question

User = get_user_model()
ANSWERS_URL = "/api/v1/answers/"


class TestAnswerEnqueueContract(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="asker", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.question = Question.objects.create(
            content="Why do you want this role?",
            created_by=self.user,
        )

    def _payload(self, ai_assist, content=None):
        attrs = {"ai_assist": ai_assist}
        if content is not None:
            attrs["content"] = content
        return {
            "data": {
                "type": "answer",
                "attributes": attrs,
                "relationships": {
                    "question": {
                        "data": {"type": "question", "id": str(self.question.id)}
                    }
                },
            }
        }

    def test_ai_assist_creates_pending_and_enqueues_answer_kind(self):
        with patch(
            "job_hunting.api.views.questions.get_client", return_value=MagicMock()
        ), patch("job_hunting.api.views.questions.enqueue") as mock_enqueue:
            resp = self.client.post(
                ANSWERS_URL,
                data=self._payload(ai_assist=True),
                content_type="application/vnd.api+json",
            )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)

        answer = Answer.objects.filter(question=self.question).first()
        self.assertIsNotNone(answer)
        self.assertEqual(answer.status, "pending")
        self.assertIsInstance(answer.id, str)

        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "answer")
        self.assertEqual(kwargs["answer_id"], answer.id)
        # No resume relationship => career-data path (resume_id None).
        self.assertIsNone(kwargs["resume_id"])
        self.assertIn("injected_prompt", kwargs)

    def test_manual_content_completes_without_enqueue(self):
        # Synchronous content write — no ai_assist, never enqueues.
        with patch("job_hunting.api.views.questions.enqueue") as mock_enqueue:
            resp = self.client.post(
                ANSWERS_URL,
                data=self._payload(ai_assist=False, content="my hand answer"),
                content_type="application/vnd.api+json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        mock_enqueue.assert_not_called()
        answer = Answer.objects.filter(question=self.question).first()
        self.assertEqual(answer.content, "my hand answer")
