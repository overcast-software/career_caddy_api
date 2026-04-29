"""JobPost is shared; per-user visibility flows through JobPostDiscovery.

Covers:
- Non-staff list view hides posts the caller has no signal on.
- Discovery alone is enough to surface a post in the list.
- Staff bypass: every JobPost shows regardless of relation.
- POST creates a Discovery on every branch (fresh, link-dedupe, fingerprint-dedupe).
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, JobPostDiscovery


User = get_user_model()


class JobPostDiscoveryListTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="p")
        self.bob = User.objects.create_user(username="bob", password="p")
        self.company = Company.objects.create(name="Acme")
        # Created by alice — bob has no signal.
        self.post = JobPost.objects.create(
            title="SRE",
            company=self.company,
            link="https://acme.example/jobs/sre",
            description="x" * 500,
            created_by=self.alice,
        )

    def _ids(self, resp):
        return {int(r["id"]) for r in resp.json()["data"]}

    def test_non_staff_without_signal_does_not_see_post(self):
        client = APIClient()
        client.force_authenticate(user=self.bob)
        resp = client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.post.id, self._ids(resp))

    def test_discovery_surfaces_post_for_non_staff(self):
        JobPostDiscovery.objects.create(
            job_post=self.post, user=self.bob, source="email"
        )
        client = APIClient()
        client.force_authenticate(user=self.bob)
        resp = client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.post.id, self._ids(resp))

    def test_staff_sees_every_post_without_relation(self):
        staff = User.objects.create_user(
            username="staff", password="p", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.post.id, self._ids(resp))


class JobPostDiscoveryCreateTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="p")
        self.bob = User.objects.create_user(username="bob", password="p")
        self.company = Company.objects.create(name="Acme")

    def _post(self, user, link, title="Engineer"):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(
            "/api/v1/job-posts/",
            {
                "data": {
                    "type": "job-post",
                    "attributes": {
                        "title": title,
                        "link": link,
                        "description": "x" * 500,
                    },
                }
            },
            format="json",
        )

    def test_fresh_create_records_discovery(self):
        resp = self._post(self.alice, "https://acme.example/jobs/a")
        self.assertEqual(resp.status_code, 201)
        post_id = int(resp.json()["data"]["id"])
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post_id=post_id, user=self.alice
            ).exists()
        )

    def test_link_dedupe_records_discovery_for_caller(self):
        # Alice ingests first.
        first = self._post(self.alice, "https://acme.example/jobs/b")
        self.assertEqual(first.status_code, 201)
        post_id = int(first.json()["data"]["id"])

        # Bob POSTs the same link → 200 echo + Bob now has a discovery.
        second = self._post(self.bob, "https://acme.example/jobs/b")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(int(second.json()["data"]["id"]), post_id)
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post_id=post_id, user=self.bob
            ).exists()
        )

    def test_repeat_post_is_idempotent(self):
        link = "https://acme.example/jobs/c"
        self._post(self.alice, link)
        self._post(self.alice, link)  # second call should not duplicate
        self.assertEqual(
            JobPostDiscovery.objects.filter(user=self.alice).count(), 1
        )
