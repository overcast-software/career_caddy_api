from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Resume

User = get_user_model()


class TestResumeModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="resumeuser", password="pass")

    def test_create_resume(self):
        r = Resume.objects.create(user=self.user, title="My Resume")
        self.assertEqual(r.title, "My Resume")
        self.assertEqual(r.user, self.user)

    def test_favorite_defaults_false(self):
        r = Resume.objects.create(user=self.user)
        self.assertFalse(r.favorite)

    def test_nullable_fields(self):
        r = Resume.objects.create()
        self.assertIsNone(r.title)
        self.assertIsNone(r.user)
        self.assertIsNone(r.file_path)

    def test_multiple_resumes_per_user(self):
        Resume.objects.create(user=self.user, title="Resume A")
        Resume.objects.create(user=self.user, title="Resume B")
        self.assertEqual(Resume.objects.filter(user=self.user).count(), 2)


class TestResumeAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="resumeapi", password="pass")
        self.client.force_authenticate(user=self.user)
        self.resume = Resume.objects.create(user=self.user, title="Main Resume")

    def test_list_resumes(self):
        response = self.client.get("/api/v1/resumes/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    def test_retrieve_resume(self):
        response = self.client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["attributes"]["title"], "Main Resume")

    def test_create_resume(self):
        payload = {
            "data": {
                "type": "resume",
                "attributes": {"title": "New Resume"},
            }
        }
        response = self.client.post("/api/v1/resumes/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])

    def test_update_resume_title(self):
        payload = {
            "data": {
                "type": "resume",
                "id": str(self.resume.id),
                "attributes": {"title": "Updated Resume"},
            }
        }
        response = self.client.patch(
            f"/api/v1/resumes/{self.resume.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resume.refresh_from_db()
        self.assertEqual(self.resume.title, "Updated Resume")

    def test_mark_favorite(self):
        payload = {
            "data": {
                "type": "resume",
                "id": str(self.resume.id),
                "attributes": {"favorite": True},
            }
        }
        response = self.client.patch(
            f"/api/v1/resumes/{self.resume.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resume.refresh_from_db()
        self.assertTrue(self.resume.favorite)

    def test_other_user_cannot_retrieve(self):
        other = User.objects.create_user(username="other_resume", password="pass")
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        response = other_client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertIn(response.status_code, [403, 404])
