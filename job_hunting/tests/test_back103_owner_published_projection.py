"""BACK-103 / CC-104 — federate_rich drives the public federated projection.

The CC-51 public federated collection carries the owner's verdict / score /
applied under ``meta.federation`` for the rich /@dough page. CC-104 widens
the gate from owner-only to ``is_owner OR user_opted_into_rich(user.id)``:
when the profile owner has ``Profile.federate_rich=True`` every visitor
(anonymous included) sees ``meta.federation`` publicly — consistent with the
fediverse Note that already publishes the same signals. With ``federate_rich``
off, only the authenticated owner keeps their own preview (the ``is_owner``
leg); anonymous + non-owner visitors stay public-safe. The enrichment is
resolved for the whole page in a bounded query count (reuses the Task D
batch resolver).
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

    def test_anonymous_sees_federation_when_rich(self):
        # federate_rich=True (seeded in setUp) → the public web profile
        # exposes meta.federation to anonymous visitors too (CC-104).
        self._seed(1, score=87, vet="Vetted Good", applied=True)
        resp = APIClient().get(self.url)
        self.assertEqual(resp.status_code, 200)
        meta = resp.json()["data"][0]["meta"]["federation"]
        self.assertEqual(meta["score"], 87)
        self.assertEqual(meta["verdict"], "Vetted Good")
        self.assertTrue(meta["applied"])

    def test_non_owner_sees_federation_when_rich(self):
        # federate_rich=True → a different authenticated visitor also sees it.
        self._seed(1, score=87, vet="Vetted Good", applied=True)
        other = User.objects.create_user(username="snoop", password="p")
        client = APIClient()
        client.force_authenticate(user=other)
        resp = client.get(self.url)
        meta = resp.json()["data"][0]["meta"]["federation"]
        self.assertEqual(meta["score"], 87)
        self.assertEqual(meta["verdict"], "Vetted Good")
        self.assertTrue(meta["applied"])

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


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestPublishedProjectionRichOff(TestCase):
    """federate_rich=False — the public web profile stays plain for every
    PUBLIC visitor; only the authenticated owner keeps their own preview via
    the ``is_owner`` leg of the CC-104 ``is_owner OR federate_rich`` gate."""

    def setUp(self):
        self.owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=self.owner, federate_rich=False)
        self.url = "/api/v1/users/dough/job-posts/federated/"
        company = Company.objects.create(name="Acme off")
        post = JobPost.objects.create(
            created_by=self.owner, title="Engineer off", description="d",
            complete=True, link="https://x.example/j/off", company=company,
            audience=[AS2_PUBLIC],
        )
        Score.objects.create(job_post=post, user=self.owner, score=91)
        app = JobApplication.objects.create(
            job_post=post, user=self.owner, applied_at=timezone.now(),
        )
        status = Status.objects.get_or_create(status="Vetted Good")[0]
        JobApplicationStatus.objects.create(
            application=app, status=status, logged_at=timezone.now()
        )

    def test_anonymous_sees_public_safe_only(self):
        resp = APIClient().get(self.url)
        self.assertEqual(resp.status_code, 200)
        resource = resp.json()["data"][0]
        self.assertNotIn("federation", resource.get("meta", {}))

    def test_non_owner_sees_public_safe_only(self):
        other = User.objects.create_user(username="snoop", password="p")
        client = APIClient()
        client.force_authenticate(user=other)
        resp = client.get(self.url)
        resource = resp.json()["data"][0]
        self.assertNotIn("federation", resource.get("meta", {}))

    def test_owner_still_sees_own_preview(self):
        client = APIClient()
        client.force_authenticate(user=self.owner)
        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        meta = resp.json()["data"][0]["meta"]["federation"]
        self.assertEqual(meta["score"], 91)
        self.assertEqual(meta["verdict"], "Vetted Good")
        self.assertTrue(meta["applied"])
