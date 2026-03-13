from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Question, Company

User = get_user_model()


class TestQuestionModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="quser", password="pass")

    def test_create_question(self):
        q = Question.objects.create(
            content="Tell me about yourself",
            created_by=self.user,
        )
        self.assertEqual(q.content, "Tell me about yourself")
        self.assertEqual(q.created_by_id, self.user.id)
        self.assertFalse(q.favorite)

    def test_favorite_default(self):
        q = Question.objects.create(content="What is your strength?", created_by=self.user)
        self.assertFalse(q.favorite)

    def test_company_fk(self):
        co = Company.objects.create(name="TestCo")
        q = Question.objects.create(content="Q?", created_by=self.user, company=co)
        q.refresh_from_db()
        self.assertEqual(q.company_id, co.id)

    def test_application_id_integer(self):
        q = Question.objects.create(content="Q?", created_by=self.user, application_id=42)
        q.refresh_from_db()
        self.assertEqual(q.application_id, 42)


class TestQuestionAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="quser2", password="pass")
        self.client.force_authenticate(user=self.user)

    def test_list_questions(self):
        Question.objects.create(content="Q1?", created_by=self.user)
        Question.objects.create(content="Q2?", created_by=self.user)
        response = self.client.get("/api/v1/questions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("data", data)
        self.assertGreaterEqual(len(data["data"]), 2)

    def test_retrieve_question(self):
        q = Question.objects.create(content="What is CI/CD?", created_by=self.user)
        response = self.client.get(f"/api/v1/questions/{q.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["data"]["id"], str(q.id))
        self.assertEqual(data["data"]["attributes"]["content"], "What is CI/CD?")

    def test_create_question(self):
        payload = {
            "data": {
                "type": "question",
                "attributes": {"content": "Describe a challenge you faced."},
            }
        }
        response = self.client.post("/api/v1/questions/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])
        data = response.json()
        self.assertEqual(data["data"]["attributes"]["content"], "Describe a challenge you faced.")

    def test_delete_question(self):
        q = Question.objects.create(content="To delete", created_by=self.user)
        response = self.client.delete(f"/api/v1/questions/{q.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Question.objects.filter(pk=q.id).exists())
