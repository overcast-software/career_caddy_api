"""BACK-102 — owner-only ``published`` state + instance publish-UI capability.

The owner sees ``meta.published`` (derived from audience ∋ AS2_PUBLIC) on
their own job-post resource; non-owners never do (no audience leak). The
publish / unpublish actions return the updated post so the frontend can
re-read state. The instance capability ``federation_publish_ui`` surfaces
on the healthcheck so the SPA can gate the publish button.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.models import JobPost, Score
from job_hunting.models.job_post import AS2_PUBLIC

User = get_user_model()

ENQUEUE = "job_hunting.signals.federation.enqueue_jobpost_activity"


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestOwnerPublishedState(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="p")
        self.other = User.objects.create_user(username="other", password="p")
        self.client = APIClient()

    def _post(self, audience):
        return JobPost.objects.create(
            created_by=self.owner, title="Engineer", description="d",
            link="https://x.example/j", audience=audience,
        )

    def test_owner_sees_published_true(self):
        post = self._post([AS2_PUBLIC])
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(f"/api/v1/job-posts/{post.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertis_published(resp, True)

    def test_owner_sees_published_false(self):
        post = self._post([])
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(f"/api/v1/job-posts/{post.id}/")
        self.assertis_published(resp, False)

    def test_non_owner_does_not_see_published(self):
        post = self._post([AS2_PUBLIC])
        # `other` gets access via a score, but is NOT the owner.
        Score.objects.create(job_post=post, user=self.other, score=50)
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(f"/api/v1/job-posts/{post.id}/")
        self.assertEqual(resp.status_code, 200)
        meta = resp.json()["data"].get("meta", {})
        self.assertNotIn("published", meta)

    def test_publish_then_unpublish_reflects_state(self):
        post = self._post([])
        self.client.force_authenticate(user=self.owner)
        with patch(ENQUEUE):
            r1 = self.client.post(f"/api/v1/job-posts/{post.id}/publish/")
            r2 = self.client.post(f"/api/v1/job-posts/{post.id}/unpublish/")
        self.assertis_published(r1, True)
        self.assertis_published(r2, False)

    def assertis_published(self, resp, expected):
        meta = resp.json()["data"].get("meta", {})
        self.assertIn("published", meta)
        self.assertEqual(meta["published"], expected)


class TestFederationPublishUICapability(TestCase):
    def test_healthcheck_exposes_capability_default(self):
        resp = self.client.get("/api/v1/healthcheck/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("federation_publish_ui", resp.json())

    @override_settings(FEDERATION_PUBLISH_UI="operator_only")
    def test_healthcheck_reports_operator_only(self):
        resp = self.client.get("/api/v1/healthcheck/")
        self.assertEqual(resp.json()["federation_publish_ui"], "operator_only")
