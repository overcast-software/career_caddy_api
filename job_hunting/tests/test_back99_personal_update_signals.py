"""BACK-99 (Task C) — fire an AP Update when the owner's rich data changes.

The first Create(Note) at ingest is always thin; the verdict / score /
applied only reach the fediverse via a later Update. These signals
re-emit an Update when the OWNER's triage / Score / JobApplication
changes on a PUBLIC, RICH-opted-in post — and emit nothing for private
posts, lean users, or another user's change on the shared row.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from job_hunting.models import (
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Profile,
    Score,
    Status,
)
from job_hunting.models.job_post import AS2_PUBLIC

User = get_user_model()

ENQUEUE = "job_hunting.signals.federation.enqueue_jobpost_activity"


def _kinds(enq):
    return [c.args[1] for c in enq.call_args_list]


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestPersonalUpdateSignals(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="p")
        Profile.objects.create(user=self.owner, federate_rich=True)

    def _post(self, *, owner=None, audience):
        return JobPost.objects.create(
            created_by=owner or self.owner,
            title="Engineer",
            description="real description",
            complete=True,
            audience=audience,
        )

    def test_score_change_on_public_rich_post_enqueues_update(self):
        with patch(ENQUEUE) as enq:
            post = self._post(audience=[AS2_PUBLIC])
            enq.reset_mock()
            Score.objects.create(job_post=post, user=self.owner, score=80)
        self.assertIn("update", _kinds(enq))

    def test_vet_status_on_public_rich_post_enqueues_update(self):
        with patch(ENQUEUE) as enq:
            post = self._post(audience=[AS2_PUBLIC])
            app = JobApplication.objects.create(job_post=post, user=self.owner)
            enq.reset_mock()
            status = Status.objects.get_or_create(status="Vetted Good")[0]
            JobApplicationStatus.objects.create(
                application=app, status=status, logged_at=timezone.now()
            )
        self.assertIn("update", _kinds(enq))

    def test_applied_at_set_enqueues_update(self):
        with patch(ENQUEUE) as enq:
            post = self._post(audience=[AS2_PUBLIC])
            app = JobApplication.objects.create(job_post=post, user=self.owner)
            enq.reset_mock()
            app.applied_at = timezone.now()
            app.save(update_fields=["applied_at"])
        self.assertIn("update", _kinds(enq))

    def test_empty_application_create_does_not_emit(self):
        # A bare triage-created JobApplication (applied_at NULL) isn't an
        # "apply" event and must not spuriously re-emit.
        with patch(ENQUEUE) as enq:
            post = self._post(audience=[AS2_PUBLIC])
            enq.reset_mock()
            JobApplication.objects.create(job_post=post, user=self.owner)
        self.assertNotIn("update", _kinds(enq))

    def test_private_post_no_emit(self):
        with patch(ENQUEUE) as enq:
            post = self._post(audience=[])
            enq.reset_mock()
            Score.objects.create(job_post=post, user=self.owner, score=90)
        self.assertNotIn("update", _kinds(enq))

    def test_lean_owner_no_emit_on_score(self):
        lean = User.objects.create_user(username="lean", password="p")
        Profile.objects.create(user=lean, federate_rich=False)
        with patch(ENQUEUE) as enq:
            post = self._post(owner=lean, audience=[AS2_PUBLIC])
            enq.reset_mock()
            Score.objects.create(job_post=post, user=lean, score=90)
        self.assertNotIn("update", _kinds(enq))

    def test_non_owner_change_no_emit(self):
        other = User.objects.create_user(username="other", password="p")
        with patch(ENQUEUE) as enq:
            post = self._post(audience=[AS2_PUBLIC])  # owned by self.owner
            enq.reset_mock()
            # `other` scores the shared public post — doesn't change what the
            # owner-attributed Note renders.
            Score.objects.create(job_post=post, user=other, score=99)
        self.assertNotIn("update", _kinds(enq))
