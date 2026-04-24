"""Regression: active_reason_code must not leak across users.

JobPost is shared. Per-user triage state (including the VB reason) lives
on JobApplicationStatus via JobApplication.user. This test locks in the
guarantee that user B never sees user A's reason on the same shared post.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company, JobApplication, JobPost, Score


User = get_user_model()


class TestJobPostReasonCodeScoping(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_a = User.objects.create_user(username="alice", password="pw")
        self.user_b = User.objects.create_user(username="bob", password="pw")
        self.company = Company.objects.create(name="Shared Inc")
        # Created by user A, but user B will reach it via having their own
        # Score — mirrors the list queryset's `scores__user_id` inclusion path.
        self.post = JobPost.objects.create(
            title="Shared Post", company=self.company, created_by=self.user_a
        )
        Score.objects.create(job_post=self.post, user=self.user_b, score=42)

    def _triage(self, user, status, reason_code=None, note=None):
        self.client.force_authenticate(user=user)
        payload = {"status": status}
        if reason_code is not None:
            payload["reason_code"] = reason_code
        if note is not None:
            payload["note"] = note
        response = self.client.post(
            f"/api/v1/job-posts/{self.post.id}/triage/",
            data=payload,
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_user_b_does_not_see_user_a_reason_on_retrieve(self):
        self._triage(self.user_a, "Vetted Bad", reason_code="compensation")

        self.client.force_authenticate(user=self.user_b)
        response = self.client.get(f"/api/v1/job-posts/{self.post.id}/")
        self.assertEqual(response.status_code, 200)
        triage = response.json()["data"]["meta"]["triage"]
        self.assertIsNone(triage["reason_code"])
        self.assertIsNone(triage["status"])

    def test_user_b_does_not_see_user_a_reason_on_list(self):
        self._triage(self.user_a, "Vetted Bad", reason_code="seniority")

        self.client.force_authenticate(user=self.user_b)
        response = self.client.get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, 200)
        match = next(
            (d for d in response.json()["data"] if int(d["id"]) == self.post.id),
            None,
        )
        self.assertIsNotNone(match, "user B should see the shared post via their Score")
        self.assertIsNone(match["meta"]["triage"]["reason_code"])
        self.assertIsNone(match["meta"]["triage"]["status"])

    def test_each_user_sees_their_own_reason(self):
        self._triage(self.user_a, "Vetted Bad", reason_code="compensation")
        # user B needs their own JobApplication to log a status against —
        # triage creates one on first call.
        self._triage(self.user_b, "Vetted Bad", reason_code="location")

        self.client.force_authenticate(user=self.user_a)
        a_view = self.client.get(f"/api/v1/job-posts/{self.post.id}/").json()
        self.assertEqual(a_view["data"]["meta"]["triage"]["reason_code"], "compensation")

        self.client.force_authenticate(user=self.user_b)
        b_view = self.client.get(f"/api/v1/job-posts/{self.post.id}/").json()
        self.assertEqual(b_view["data"]["meta"]["triage"]["reason_code"], "location")

        # And two separate applications exist, one per user.
        self.assertEqual(
            JobApplication.objects.filter(job_post=self.post).count(), 2
        )
