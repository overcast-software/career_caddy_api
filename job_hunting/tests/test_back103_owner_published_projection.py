"""BACK-103 (Task E) — owner-published federated projection exposes
verdict / score / applied.

The CC-51 public federated collection stays public-safe for anonymous /
non-owner visitors, but when the OWNER views their own page (SPA sends
its JWT on this AllowAny route) each published post carries the owner's
verdict / score / applied under ``meta.federation`` for the rich /@dough
page. The enrichment is resolved for the whole page in a bounded query
count (reuses the Task D batch resolver).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Profile,
    Score,
    Status,
)
from job_hunting.models.job_post import AS2_PUBLIC

User = get_user_model()


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestOwnerPublishedProjection(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=self.owner, federate_rich=True)
        self.url = "/api/v1/users/dough/job-posts/federated/"

    def _seed(self, n, *, score=None, vet=None, applied=False):
        company = Company.objects.create(name=f"Acme {n}")
        post = JobPost.objects.create(
            created_by=self.owner, title=f"Engineer {n}", description="d",
            complete=True, link=f"https://x.example/j/{n}", company=company,
            audience=[AS2_PUBLIC],
        )
        if score is not None:
            Score.objects.create(job_post=post, user=self.owner, score=score)
        if vet is not None:
            app = JobApplication.objects.create(
                job_post=post, user=self.owner,
                applied_at=timezone.now() if applied else None,
            )
            status = Status.objects.get_or_create(status=vet)[0]
            JobApplicationStatus.objects.create(
                application=app, status=status, logged_at=timezone.now()
            )
        return post

    def test_owner_sees_federation_annotations(self):
        self._seed(1, score=87, vet="Vetted Good", applied=True)
        client = APIClient()
        client.force_authenticate(user=self.owner)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        meta = resp.json()["data"][0]["meta"]["federation"]
        self.assertEqual(meta["score"], 87)
        self.assertEqual(meta["verdict"], "Vetted Good")
        self.assertTrue(meta["applied"])

    def test_anonymous_gets_public_safe_only(self):
        self._seed(1, score=87, vet="Vetted Good", applied=True)
        resp = APIClient().get(self.url)
        self.assertEqual(resp.status_code, 200)
        resource = resp.json()["data"][0]
        self.assertNotIn("federation", resource.get("meta", {}))

    def test_non_owner_gets_public_safe_only(self):
        self._seed(1, score=87, vet="Vetted Good", applied=True)
        other = User.objects.create_user(username="snoop", password="p")
        client = APIClient()
        client.force_authenticate(user=other)
        resp = client.get(self.url)
        resource = resp.json()["data"][0]
        self.assertNotIn("federation", resource.get("meta", {}))

    def test_owner_projection_query_count_constant(self):
        self._seed(1, score=80, vet="Vetted Good")
        self._seed(2, score=70, vet="Vetted Bad")
        client = APIClient()
        client.force_authenticate(user=self.owner)
        with CaptureQueriesContext(connection) as small:
            client.get(self.url)
        q_small = len(small)

        for i in range(3, 7):
            self._seed(i, score=60 + i, vet="Vetted Good")
        with CaptureQueriesContext(connection) as big:
            client.get(self.url)
        q_big = len(big)

        self.assertEqual(
            q_small, q_big,
            f"owner projection N+1'd: {q_small} (2) vs {q_big} (6)",
        )
