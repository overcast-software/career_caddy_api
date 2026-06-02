"""Phase 5d federation signal-hook tests.

Asserts the JobPost ↔ enqueue_jobpost_activity wiring: which save /
delete transitions fire a Create vs Update vs Delete fanout, and which
don't. The dispatch internals are stubbed — we only care that
``enqueue_jobpost_activity`` is called with the right arguments.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from job_hunting.models import Actor, FederationFollower, JobPost
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


def _seed_setup(username="dough"):
    user = User.objects.create_user(username=username, password="pass")
    Actor.objects.create(
        preferred_username=username,
        type="Person",
        user=user,
        private_key_pem="-----PRETEND-----",
        public_key_pem="-----PRETEND-----",
    )
    FederationFollower.objects.create(
        local_user=user,
        actor_uri="https://peer.example/users/alice",
        inbox_uri="https://peer.example/users/alice/inbox",
        shared_inbox_uri="https://peer.example/inbox",
        instance_host="peer.example",
        accepted_at=timezone.now(),
    )
    return user


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestSaveSignals(TestCase):
    """post_save fanout: Create on public creation / transition,
    Update on content edit, nothing on administrative edit."""

    def setUp(self):
        self.user = _seed_setup()

    def test_public_create_enqueues_create(self):
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            JobPost.objects.create(
                created_by=self.user,
                title="A",
                description="B",
                link="https://example.com/jobs/1",
            )
        # Find the create call
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertIn("create", kinds)

    def test_private_create_does_not_enqueue(self):
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            JobPost.objects.create(
                created_by=self.user,
                title="Private",
                description="Notes",
                link="https://example.com/private/1",
                audience=[],
            )
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertNotIn("create", kinds)
        self.assertNotIn("update", kinds)

    def test_private_to_public_transition_enqueues_create(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Hidden",
            description="for now",
            link="https://example.com/jobs/2",
            audience=[],
        )
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            post.audience = [AS2_PUBLIC]
            post.save()
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertIn("create", kinds)
        self.assertNotIn("update", kinds)

    def test_public_to_public_title_change_enqueues_update(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Engineer",
            description="x",
            link="https://example.com/jobs/3",
        )
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            post.title = "Senior Engineer"
            post.save()
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertEqual(kinds, ["update"])

    def test_public_to_public_administrative_change_does_not_enqueue(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Engineer",
            description="x",
            link="https://example.com/jobs/4",
        )
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            # apply_url_status is administrative — not in the whitelist
            post.apply_url_status = "resolved"
            post.save()
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertEqual(kinds, [])

    def test_public_to_private_does_not_enqueue(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Engineer",
            description="x",
            link="https://example.com/jobs/5",
        )
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            post.audience = []
            post.save()
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertEqual(kinds, [])

    def test_description_change_enqueues_update(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Engineer",
            description="initial",
            link="https://example.com/jobs/6",
        )
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            post.description = "revised description"
            post.save()
        kinds = [call.args[1] for call in enq.call_args_list]
        self.assertEqual(kinds, ["update"])

    def test_update_passes_edit_marker(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Engineer",
            description="x",
            link="https://example.com/jobs/7",
        )
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity"
        ) as enq:
            post.title = "Engineer II"
            post.save()
        # Locate the update call and inspect kwargs
        update_calls = [c for c in enq.call_args_list if c.args[1] == "update"]
        self.assertEqual(len(update_calls), 1)
        self.assertIn("edit_marker", update_calls[0].kwargs)


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestDeleteSignals(TestCase):
    """post_delete fanout: Delete only for was-public rows."""

    def setUp(self):
        self.user = _seed_setup()

    def test_delete_of_public_enqueues_delete(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Bye",
            description="leaving",
            link="https://example.com/jobs/d1",
        )
        with patch(
            "job_hunting.signals.federation._enqueue_delete_for_instance"
        ) as enq:
            post.delete()
        enq.assert_called_once()

    def test_delete_of_private_does_not_enqueue(self):
        post = JobPost.objects.create(
            created_by=self.user,
            title="Private",
            description="x",
            link="https://example.com/jobs/d2",
            audience=[],
        )
        with patch(
            "job_hunting.signals.federation._enqueue_delete_for_instance"
        ) as enq:
            post.delete()
        enq.assert_not_called()


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestSignalsNoFollowers(TestCase):
    """Signals still fire when no followers exist; dispatch helper returns 0."""

    def test_create_without_followers_calls_enqueue_returns_zero(self):
        user = _seed_setup()
        FederationFollower.objects.all().delete()
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity",
            return_value=0,
        ) as enq:
            JobPost.objects.create(
                created_by=user,
                title="Solo",
                description="x",
                link="https://example.com/jobs/solo",
            )
        # Signal still fired the enqueue, even though no followers exist —
        # the gating is in the dispatch helper, not the signal.
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertIn("create", kinds)
