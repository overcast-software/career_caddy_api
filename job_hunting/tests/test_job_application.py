from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Company, JobPost, JobApplication

User = get_user_model()


class TestJobApplicationModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="jauser", password="pass")
        self.company = Company.objects.create(name="Acme")
        self.job_post = JobPost.objects.create(title="Dev", company=self.company)

    def test_create_application(self):
        ja = JobApplication.objects.create(
            user=self.user, job_post=self.job_post, company=self.company
        )
        self.assertEqual(ja.user, self.user)
        self.assertEqual(ja.job_post, self.job_post)

    def test_nullable_fields(self):
        ja = JobApplication.objects.create()
        self.assertIsNone(ja.user)
        self.assertIsNone(ja.status)
        self.assertIsNone(ja.applied_at)

    def test_company_relationship(self):
        ja = JobApplication.objects.create(company=self.company, user=self.user)
        self.assertIn(ja, list(self.company.applications.all()))


class TestJobApplicationAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="jaapi", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="WidgetCo")
        self.job_post = JobPost.objects.create(
            title="SWE", company=self.company, created_by=self.user
        )
        self.application = JobApplication.objects.create(
            user=self.user, job_post=self.job_post, company=self.company
        )

    def test_list_applications(self):
        response = self.client.get("/api/v1/job-applications/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    def test_retrieve_application(self):
        response = self.client.get(f"/api/v1/job-applications/{self.application.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_application(self):
        payload = {
            "data": {
                "type": "job-application",
                "attributes": {"status": "applied"},
                "relationships": {
                    "job-post": {"data": {"type": "job-post", "id": str(self.job_post.id)}},
                },
            }
        }
        response = self.client.post("/api/v1/job-applications/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])

    def test_update_application_status(self):
        payload = {
            "data": {
                "type": "job-application",
                "id": str(self.application.id),
                "attributes": {"status": "interviewing"},
            }
        }
        response = self.client.patch(
            f"/api/v1/job-applications/{self.application.id}/",
            data=payload,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, "interviewing")

    def test_delete_application(self):
        ja = JobApplication.objects.create(user=self.user)
        response = self.client.delete(f"/api/v1/job-applications/{ja.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(JobApplication.objects.filter(pk=ja.id).exists())

    def test_other_user_cannot_retrieve(self):
        other = User.objects.create_user(username="other_ja", password="pass")
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        response = other_client.get(f"/api/v1/job-applications/{self.application.id}/")
        self.assertIn(response.status_code, [403, 404])
