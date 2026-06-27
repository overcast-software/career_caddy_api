"""CC-103 — question/answer list ordering must be recency, not lexical PK.

Question and Answer carry NanoID string PKs (CC-77), which sort lexically
rather than by insertion order. The list endpoints previously ordered by
``-id`` as their sole key, so the "newest first" intent silently became a
reverse-lexical (effectively random) order once the PKs stopped being
auto-increment integers. These tests pin recency ordering (``-created_at``,
``-id`` tiebreak).

``created_at`` is ``auto_now_add``; we stamp explicit, well-separated
timestamps via ``update()`` (which bypasses ``auto_now_add``) so the
expected order is deterministic and independent of the random PKs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Answer, Question

User = get_user_model()

QUESTIONS_URL = "/api/v1/questions/"
ANSWERS_URL = "/api/v1/answers/"
_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_timezone.utc)


class TestQuestionListRecencyOrdering(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="quizzer", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # Create q1..q4, then stamp ascending created_at so newest-first is
        # [q4, q3, q2, q1] regardless of the random NanoID PKs.
        self.questions = []
        for n in range(1, 5):
            q = Question.objects.create(created_by=self.user, content=f"Q{n}")
            Question.objects.filter(pk=q.pk).update(
                created_at=_BASE + timedelta(minutes=n)
            )
            q.refresh_from_db()
            self.questions.append(q)
        self.expected_newest_first = [str(q.id) for q in reversed(self.questions)]

    def test_questions_listed_newest_first_by_created_at(self):
        resp = self.client.get(QUESTIONS_URL)
        self.assertEqual(resp.status_code, 200)
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertEqual(
            ids,
            self.expected_newest_first,
            "questions must be ordered newest-first by created_at, not by "
            "lexical NanoID PK",
        )


class TestAnswerListRecencyOrdering(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="answerer", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.question = Question.objects.create(
            created_by=self.user, content="parent"
        )
        # Create a1..a4 under the question, then stamp ascending created_at.
        self.answers = []
        for n in range(1, 5):
            a = Answer.objects.create(question=self.question, content=f"A{n}")
            Answer.objects.filter(pk=a.pk).update(
                created_at=_BASE + timedelta(minutes=n)
            )
            a.refresh_from_db()
            self.answers.append(a)
        self.expected_newest_first = [str(a.id) for a in reversed(self.answers)]

    def test_answers_listed_newest_first_by_created_at(self):
        resp = self.client.get(ANSWERS_URL)
        self.assertEqual(resp.status_code, 200)
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertEqual(
            ids,
            self.expected_newest_first,
            "answers must be ordered newest-first by created_at, not by "
            "lexical NanoID PK",
        )
