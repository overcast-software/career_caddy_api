"""GET /api/v1/job-posts/ default-hides closed posts; ?include_closed=true
opts back in. NULL status (historical) always passes through.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost


User = get_user_model()


class JobPostApplicationStatusFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.open_post = JobPost.objects.create(
            title="Open Engineer",
            company=self.company,
            link="https://acme.example/jobs/1",
            description="x" * 500,
            application_status="open",
            created_by=self.user,
        )
        self.closed_post = JobPost.objects.create(
            title="Closed Engineer",
            company=self.company,
            link="https://acme.example/jobs/2",
            description="x" * 500,
            application_status="closed",
            created_by=self.user,
        )
        self.unknown_post = JobPost.objects.create(
            title="Unknown Engineer",
            company=self.company,
            link="https://acme.example/jobs/3",
            description="x" * 500,
            application_status=None,
            created_by=self.user,
        )

    def _ids(self, resp):
        return {int(r["id"]) for r in resp.json()["data"]}

    def test_default_hides_closed_includes_open_and_unknown(self):
        resp = self.client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        ids = self._ids(resp)
        self.assertIn(self.open_post.id, ids)
        self.assertIn(self.unknown_post.id, ids)
        self.assertNotIn(self.closed_post.id, ids)

    def test_include_closed_shows_everything(self):
        resp = self.client.get("/api/v1/job-posts/?include_closed=true")
        self.assertEqual(resp.status_code, 200)
        ids = self._ids(resp)
        self.assertEqual(
            ids,
            {self.open_post.id, self.closed_post.id, self.unknown_post.id},
        )

    def test_explicit_closed_filter_returns_only_closed(self):
        resp = self.client.get(
            "/api/v1/job-posts/?filter[application_status]=closed"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {self.closed_post.id})

    def test_explicit_open_filter_returns_only_open(self):
        resp = self.client.get(
            "/api/v1/job-posts/?filter[application_status]=open"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {self.open_post.id})

    def test_application_status_in_serialized_attributes(self):
        resp = self.client.get(f"/api/v1/job-posts/{self.closed_post.id}/")
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["application_status"], "closed")
