"""CC-62 (A1) — owner-only ``publish`` / ``unpublish`` actions on
JobPostViewSet.

Exercises the BACK-91 audience-transition contract: publishing ensures
``AS2_PUBLIC`` is in ``audience`` (private→public ⇒ Create fanout);
unpublishing removes it (public→private ⇒ nothing). The signal handler
enqueues via ``job_hunting.signals.federation.enqueue_jobpost_activity``,
which we patch to assert the fanout decision without exercising dispatch
internals (same seam as test_back91_private_ingestion_publish_optin).
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.models import JobPost
from job_hunting.models.job_post import AS2_PUBLIC

User = get_user_model()

# The signal handler enqueues via this symbol; patching it lets us assert
# the fanout decision without exercising dispatch internals.
ENQUEUE = "job_hunting.signals.federation.enqueue_jobpost_activity"


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestPublishUnpublishActions(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.client = APIClient()

    def _make_post(self, *, audience=None, created_by=None):
        return JobPost.objects.create(
            created_by=created_by or self.owner,
            title="Engineer",
            description="Build",
            audience=[] if audience is None else audience,
        )

    def test_publish_adds_public_and_enqueues_create(self):
        post = self._make_post()
        self.client.force_authenticate(user=self.owner)
        with patch(ENQUEUE) as enq:
            resp = self.client.post(f"/api/v1/job-posts/{post.id}/publish/")
        self.assertEqual(resp.status_code, 200)
        post.refresh_from_db()
        self.assertIn(AS2_PUBLIC, post.audience)
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertIn("create", kinds)

    def test_unpublish_removes_public_and_enqueues_nothing(self):
        post = self._make_post(audience=[AS2_PUBLIC])
        self.client.force_authenticate(user=self.owner)
        with patch(ENQUEUE) as enq:
            resp = self.client.post(f"/api/v1/job-posts/{post.id}/unpublish/")
        self.assertEqual(resp.status_code, 200)
        post.refresh_from_db()
        self.assertNotIn(AS2_PUBLIC, post.audience)
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertNotIn("create", kinds)
        self.assertNotIn("update", kinds)

    def test_unpublish_preserves_other_audience_entries(self):
        followers = "http://testserver/actors/owner/followers"
        post = self._make_post(audience=[AS2_PUBLIC, followers])
        self.client.force_authenticate(user=self.owner)
        with patch(ENQUEUE):
            resp = self.client.post(f"/api/v1/job-posts/{post.id}/unpublish/")
        self.assertEqual(resp.status_code, 200)
        post.refresh_from_db()
        self.assertEqual(post.audience, [followers])

    def test_publish_twice_is_idempotent_single_create(self):
        post = self._make_post()
        self.client.force_authenticate(user=self.owner)
        with patch(ENQUEUE) as enq:
            r1 = self.client.post(f"/api/v1/job-posts/{post.id}/publish/")
            r2 = self.client.post(f"/api/v1/job-posts/{post.id}/publish/")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        post.refresh_from_db()
        self.assertIn(AS2_PUBLIC, post.audience)
        creates = [c for c in enq.call_args_list if c.args[1] == "create"]
        self.assertEqual(len(creates), 1)

    def test_non_owner_forbidden(self):
        post = self._make_post(created_by=self.owner)
        self.client.force_authenticate(user=self.other)
        with patch(ENQUEUE) as enq:
            resp = self.client.post(f"/api/v1/job-posts/{post.id}/publish/")
        self.assertIn(resp.status_code, (403, 404))
        post.refresh_from_db()
        self.assertNotIn(AS2_PUBLIC, post.audience)
        self.assertEqual(enq.call_args_list, [])

    def test_publish_enters_public_queryset_unpublish_leaves(self):
        from job_hunting.api.views.federation import (
            public_jobpost_queryset_for_user,
        )

        post = self._make_post()
        self.client.force_authenticate(user=self.owner)

        with patch(ENQUEUE):
            self.client.post(f"/api/v1/job-posts/{post.id}/publish/")
        ids = list(
            public_jobpost_queryset_for_user(self.owner.id).values_list(
                "id", flat=True
            )
        )
        self.assertIn(post.id, ids)

        with patch(ENQUEUE):
            self.client.post(f"/api/v1/job-posts/{post.id}/unpublish/")
        ids_after = list(
            public_jobpost_queryset_for_user(self.owner.id).values_list(
                "id", flat=True
            )
        )
        self.assertNotIn(post.id, ids_after)
