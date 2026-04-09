from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Answer, Question

User = get_user_model()


class TestAnswerModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="answeruser", password="pass")
        self.question = Question.objects.create(
            content="What is your experience with Python?",
            created_by=self.user,
        )

    def test_create_answer(self):
        a = Answer.objects.create(question=self.question, content="5 years")
        self.assertEqual(a.content, "5 years")
        self.assertEqual(a.question, self.question)

    def test_favorite_defaults_false(self):
        a = Answer.objects.create(question=self.question, content="test")
        self.assertFalse(a.favorite)

    def test_nullable_content(self):
        a = Answer.objects.create(question=self.question)
        self.assertIsNone(a.content)


class TestAnswerAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="answerapi", password="pass")
        self.client.force_authenticate(user=self.user)
        self.question = Question.objects.create(
            content="Describe a challenge you overcame.",
            created_by=self.user,
        )
        self.answer = Answer.objects.create(
            question=self.question,
            content="I refactored a legacy codebase.",
        )

    def test_list_answers(self):
        response = self.client.get("/api/v1/answers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    def test_retrieve_answer(self):
        response = self.client.get(f"/api/v1/answers/{self.answer.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_answer_manually(self):
        payload = {
            "data": {
                "type": "answer",
                "attributes": {"content": "Manually written answer"},
                "relationships": {
                    "question": {"data": {"type": "question", "id": str(self.question.id)}},
                },
            }
        }
        response = self.client.post("/api/v1/answers/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])

    def test_update_answer_content(self):
        payload = {
            "data": {
                "type": "answer",
                "id": str(self.answer.id),
                "attributes": {"content": "Updated answer content"},
            }
        }
        response = self.client.patch(
            f"/api/v1/answers/{self.answer.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_mark_answer_favorite(self):
        payload = {
            "data": {
                "type": "answer",
                "id": str(self.answer.id),
                "attributes": {"favorite": True},
            }
        }
        response = self.client.patch(
            f"/api/v1/answers/{self.answer.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.answer.refresh_from_db()
        self.assertTrue(self.answer.favorite)

    def test_delete_answer(self):
        a = Answer.objects.create(question=self.question, content="to delete")
        response = self.client.delete(f"/api/v1/answers/{a.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Answer.objects.filter(pk=a.id).exists())
