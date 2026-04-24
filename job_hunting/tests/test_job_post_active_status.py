from datetime import timedelta

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Status,
)


User = get_user_model()


def _log(app, status_name, *, days_ago):
    return JobApplicationStatus.objects.create(
        application=app,
        status=Status.objects.get_or_create(status=status_name)[0],
        logged_at=timezone.now() - timedelta(days=days_ago),
    )


def _triage(client, post_id):
    """Per-caller triage summary lives in JSON:API `meta.triage`, not
    `attributes`. Shape: { status, reason_code, note }."""
    response = client.get(f"/api/v1/job-posts/{post_id}/")
    return response.json()["data"].get("meta", {}).get("triage", {})


class TestJobPostActiveApplicationStatus(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ase", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="Eng", company=self.company, created_by=self.user
        )

    def test_returns_none_when_no_application(self):
        triage = _triage(self.client, self.post.id)
        self.assertIsNone(triage["status"])

    def test_returns_latest_status_name(self):
        app = JobApplication.objects.create(job_post=self.post, user=self.user)
        _log(app, "Applied", days_ago=10)
        _log(app, "Interview Scheduled", days_ago=2)
        triage = _triage(self.client, self.post.id)
        self.assertEqual(triage["status"], "Interview Scheduled")

    def test_ignores_other_users_applications(self):
        their_app = JobApplication.objects.create(job_post=self.post, user=self.other)
        _log(their_app, "Offer", days_ago=1)
        triage = _triage(self.client, self.post.id)
        self.assertIsNone(triage["status"])

    def test_list_endpoint_includes_active_status(self):
        app = JobApplication.objects.create(job_post=self.post, user=self.user)
        _log(app, "Applied", days_ago=3)
        response = self.client.get("/api/v1/job-posts/")
        rows = response.json()["data"]
        match = next(r for r in rows if r["id"] == str(self.post.id))
        self.assertEqual(match["meta"]["triage"]["status"], "Applied")
