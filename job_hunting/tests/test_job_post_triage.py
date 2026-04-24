from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
)


User = get_user_model()


class TestJobPostTriageAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="tri", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="Eng", company=self.company, created_by=self.user
        )
        self.url = f"/api/v1/job-posts/{self.post.id}/triage/"

    def test_creates_application_and_status_on_first_triage(self):
        response = self.client.post(self.url, data={"status": "Vetted Good"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(JobApplication.objects.filter(job_post=self.post, user=self.user).count(), 1)
        app = JobApplication.objects.get(job_post=self.post, user=self.user)
        statuses = list(JobApplicationStatus.objects.filter(application=app))
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].status.status, "Vetted Good")
        self.assertEqual(app.status, "Vetted Good")
        self.assertEqual(
            response.json()["data"]["meta"]["triage"]["status"],
            "Vetted Good",
        )

    def test_triage_stores_note_on_application_status(self):
        self.client.post(
            self.url,
            data={"status": "Vetted Bad", "note": "Salary too low"},
            format="json",
        )
        app = JobApplication.objects.get(job_post=self.post, user=self.user)
        jas = JobApplicationStatus.objects.get(application=app)
        self.assertEqual(jas.note, "Salary too low")

    def test_triage_updates_application_status_cache(self):
        self.client.post(self.url, data={"status": "Vetted Good"}, format="json")
        self.client.post(self.url, data={"status": "Vetted Bad"}, format="json")
        app = JobApplication.objects.get(job_post=self.post, user=self.user)
        self.assertEqual(app.status, "Vetted Bad")
        self.assertEqual(JobApplicationStatus.objects.filter(application=app).count(), 2)

    def test_reuses_existing_application(self):
        app = JobApplication.objects.create(job_post=self.post, user=self.user)
        self.client.post(self.url, data={"status": "Vetted Bad"}, format="json")
        self.assertEqual(JobApplication.objects.filter(job_post=self.post, user=self.user).count(), 1)
        self.assertEqual(JobApplicationStatus.objects.filter(application=app).count(), 1)

    def test_rejects_unknown_status(self):
        response = self.client.post(self.url, data={"status": "Chocolate"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_missing_status(self):
        response = self.client.post(self.url, data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_404_for_unknown_post(self):
        response = self.client.post(
            "/api/v1/job-posts/9999/triage/",
            data={"status": "Vetted Good"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_vetted_bad_with_reason_code_is_stored(self):
        response = self.client.post(
            self.url,
            data={"status": "Vetted Bad", "reason_code": "compensation"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        app = JobApplication.objects.get(job_post=self.post, user=self.user)
        jas = JobApplicationStatus.objects.get(application=app)
        self.assertEqual(jas.reason_code, "compensation")
        self.assertEqual(
            response.json()["data"]["meta"]["triage"]["reason_code"],
            "compensation",
        )

    def test_rejects_unknown_reason_code(self):
        response = self.client.post(
            self.url,
            data={"status": "Vetted Bad", "reason_code": "bogus"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_reason_requires_note(self):
        response = self.client.post(
            self.url,
            data={"status": "Vetted Bad", "reason_code": "other"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_reason_with_note_is_stored(self):
        response = self.client.post(
            self.url,
            data={
                "status": "Vetted Bad",
                "reason_code": "other",
                "note": "Weird vibes on the JD",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        app = JobApplication.objects.get(job_post=self.post, user=self.user)
        jas = JobApplicationStatus.objects.get(application=app)
        self.assertEqual(jas.reason_code, "other")
        self.assertEqual(jas.note, "Weird vibes on the JD")

    def test_vetted_good_ignores_reason_code(self):
        self.client.post(
            self.url,
            data={"status": "Vetted Good", "reason_code": "compensation"},
            format="json",
        )
        app = JobApplication.objects.get(job_post=self.post, user=self.user)
        jas = JobApplicationStatus.objects.get(application=app)
        self.assertIsNone(jas.reason_code)
