from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Company, JobPost

User = get_user_model()


class TestJobPostModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="jpuser", password="pass")
        self.company = Company.objects.create(name="Acme")

    def test_create_job_post(self):
        jp = JobPost.objects.create(title="Engineer", company=self.company, created_by=self.user)
        self.assertEqual(jp.title, "Engineer")
        self.assertEqual(jp.company, self.company)

    def test_nullable_fields(self):
        jp = JobPost.objects.create()
        self.assertIsNone(jp.title)
        self.assertIsNone(jp.description)
        self.assertIsNone(jp.company)

    def test_link_unique(self):
        JobPost.objects.create(link="https://example.com/job/1")
        with self.assertRaises(Exception):
            JobPost.objects.create(link="https://example.com/job/1")


class TestJobPostAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="jpapi", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="TestCo")
        self.job_post = JobPost.objects.create(
            title="Dev", company=self.company, created_by=self.user
        )

    def test_list_job_posts(self):
        response = self.client.get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    def test_retrieve_job_post(self):
        response = self.client.get(f"/api/v1/job-posts/{self.job_post.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["attributes"]["title"], "Dev")

    def test_create_job_post(self):
        payload = {
            "data": {
                "type": "job-post",
                "attributes": {"title": "QA Engineer", "description": "Test everything"},
            }
        }
        response = self.client.post("/api/v1/job-posts/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])

    def test_update_job_post(self):
        payload = {
            "data": {
                "type": "job-post",
                "id": str(self.job_post.id),
                "attributes": {"title": "Senior Dev"},
            }
        }
        response = self.client.patch(
            f"/api/v1/job-posts/{self.job_post.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.job_post.refresh_from_db()
        self.assertEqual(self.job_post.title, "Senior Dev")

    def test_delete_job_post(self):
        jp = JobPost.objects.create(title="ToDelete", created_by=self.user)
        response = self.client.delete(f"/api/v1/job-posts/{jp.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(JobPost.objects.filter(pk=jp.id).exists())

    def test_retrieve_requires_auth(self):
        anon = APIClient()
        response = anon.get(f"/api/v1/job-posts/{self.job_post.id}/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
