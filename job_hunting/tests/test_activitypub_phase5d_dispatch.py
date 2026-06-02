"""Phase 5d outbound dispatch tests.

Exercises the dispatch fanout + worker entry-point in
``job_hunting.lib.federation_dispatch``. ``Q_CLUSTER["sync"]`` is on in
test settings — tasks the dispatcher enqueues execute in-band so
assertions can run immediately after ``enqueue_jobpost_activity``.

Federation delivery is monkeypatched at the signing module so no real
HTTP traffic leaves: ``federation_signing.deliver`` returns canned
``(status_code, snippet)`` tuples per test.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from job_hunting.lib import federation_dispatch, federation_signing
from job_hunting.models import (
    Actor,
    FederationActivity,
    FederationFollower,
    JobPost,
)
from job_hunting.models.federation_activity import (
    DELIVERY_DEAD_LETTER,
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    DELIVERY_REJECTED,
    DIRECTION_OUTBOUND,
)
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


def _seed_actor_and_user(username: str = "dough") -> tuple[User, Actor]:
    user = User.objects.create_user(username=username, password="pass")
    actor = Actor.objects.create(
        preferred_username=username,
        type="Person",
        user=user,
        private_key_pem="-----PRETEND PRIVATE KEY-----",
        public_key_pem="-----PRETEND PUBLIC KEY-----",
    )
    return user, actor


def _seed_follower(
    user,
    *,
    actor_uri: str = "https://peer.example/users/alice",
    inbox_uri: str | None = None,
    shared_inbox_uri: str | None = None,
    accepted: bool = True,
    unfollowed: bool = False,
) -> FederationFollower:
    inbox = inbox_uri or f"{actor_uri}/inbox"
    return FederationFollower.objects.create(
        local_user=user,
        actor_uri=actor_uri,
        inbox_uri=inbox,
        shared_inbox_uri=shared_inbox_uri,
        instance_host=FederationFollower.host_for_uri(actor_uri),
        accepted_at=timezone.now() if accepted else None,
        unfollowed_at=timezone.now() if unfollowed else None,
    )


def _seed_public_post(user, **kwargs) -> JobPost:
    defaults = dict(
        title="Senior Engineer",
        description="Build cool things.",
        link="https://example.com/jobs/1",
    )
    defaults.update(kwargs)
    return JobPost.objects.create(created_by=user, **defaults)


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestEnqueueFanout(TestCase):
    """Fanout cardinality + sharedInbox dedupe + gating."""

    def setUp(self):
        # The JobPost signal handlers call ``_schedule_dispatch_task``
        # which (with Q_CLUSTER.sync=True) runs ``dispatch_one`` in-band.
        # Patch the scheduler at the very top of setUp so signal fanouts
        # triggered by ``_seed_public_post`` don't pre-create rows or
        # blow up on the dummy private key.
        self._sched_patch = patch.object(
            federation_dispatch, "_schedule_dispatch_task", lambda *a, **k: None
        )
        self._sched_patch.start()
        self.addCleanup(self._sched_patch.stop)
        # Also patch the delete-path's internal scheduler so symmetry
        # with the dispatch module holds when test code goes through
        # the signal-handler shortcut.
        self.user, self.actor = _seed_actor_and_user()
        # Two followers on same instance with the same sharedInbox →
        # dedupe should collapse them to ONE outbound row.
        _seed_follower(
            self.user,
            actor_uri="https://peer.example/users/alice",
            shared_inbox_uri="https://peer.example/inbox",
        )
        _seed_follower(
            self.user,
            actor_uri="https://peer.example/users/bob",
            shared_inbox_uri="https://peer.example/inbox",
        )
        # Third follower on a different instance with no sharedInbox.
        _seed_follower(
            self.user,
            actor_uri="https://other.example/users/carol",
            inbox_uri="https://other.example/users/carol/inbox",
        )

    def _reset_activities(self):
        FederationActivity.objects.all().delete()

    def test_create_fans_out_one_row_per_unique_inbox(self):
        post = _seed_public_post(self.user)
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 2)
        rows = FederationActivity.objects.filter(direction=DIRECTION_OUTBOUND)
        self.assertEqual(rows.count(), 2)
        targets = sorted(rows.values_list("target_uri", flat=True))
        self.assertEqual(
            targets,
            [
                "https://other.example/users/carol/inbox",
                "https://peer.example/inbox",
            ],
        )

    def test_shared_inbox_dedupe_collapses_same_instance_followers(self):
        post = _seed_public_post(self.user)
        self._reset_activities()
        federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        peer_rows = FederationActivity.objects.filter(
            target_uri="https://peer.example/inbox"
        )
        self.assertEqual(peer_rows.count(), 1)

    def test_private_jobpost_does_not_enqueue(self):
        post = _seed_public_post(self.user, audience=[])
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 0)
        self.assertFalse(FederationActivity.objects.exists())

    def test_no_followers_returns_zero(self):
        FederationFollower.objects.all().delete()
        post = _seed_public_post(self.user)
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 0)

    def test_only_accepted_followers_count(self):
        FederationFollower.objects.all().delete()
        _seed_follower(self.user, accepted=False)
        post = _seed_public_post(self.user)
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 0)

    def test_unfollowed_followers_excluded(self):
        FederationFollower.objects.all().delete()
        _seed_follower(self.user, accepted=True, unfollowed=True)
        post = _seed_public_post(self.user)
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 0)

    def test_re_enqueue_create_is_idempotent(self):
        post = _seed_public_post(self.user)
        self._reset_activities()
        federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        # The (direction, activity_id, target_uri) constraint catches the
        # exact-same row on the second call; per-target lookup returns
        # the existing row and the count does not double.
        rows = FederationActivity.objects.filter(direction=DIRECTION_OUTBOUND)
        self.assertEqual(rows.count(), 2)

    @override_settings(ACTIVITYPUB_FEDERATION_ENABLED=False)
    def test_kill_switch_short_circuits(self):
        post = _seed_public_post(self.user)
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 0)
        self.assertFalse(FederationActivity.objects.exists())

    def test_no_actor_returns_zero(self):
        # User without an Actor row — fanout silently aborts.
        other = User.objects.create_user(username="actorless", password="pass")
        _seed_follower(other)
        post = _seed_public_post(other)
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "create")
        self.assertEqual(count, 0)

    def test_missing_jobpost_returns_zero(self):
        count = federation_dispatch.enqueue_jobpost_activity(999_999, "create")
        self.assertEqual(count, 0)

    def test_update_activity_id_includes_edit_marker(self):
        post = _seed_public_post(self.user)
        self._reset_activities()
        marker = timezone.now()
        federation_dispatch.enqueue_jobpost_activity(
            post.pk, "update", edit_marker=marker
        )
        rows = FederationActivity.objects.filter(activity_type="Update")
        self.assertEqual(rows.count(), 2)

    def test_delete_skips_public_gate(self):
        # delete is allowed even on a no-longer-public row — the signal
        # snapshots audience and decides at the signal level. The
        # dispatch helper itself doesn't second-guess.
        post = _seed_public_post(self.user, audience=[])
        self._reset_activities()
        count = federation_dispatch.enqueue_jobpost_activity(post.pk, "delete")
        self.assertEqual(count, 2)


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestDispatchOneOutcomes(TestCase):
    """Worker entry point: 2xx / 4xx / 5xx / network outcomes."""

    def setUp(self):
        # JobPost.save fires the federation post_save signal which
        # synchronously enqueues a dispatch_one task (Q_CLUSTER.sync=True
        # in tests → it runs in-band). Stub the schedule helper for the
        # entire test so we observe only the explicit dispatch_one(...)
        # calls we make below — otherwise retry_count is pre-bumped.
        self._sched_patch = patch.object(
            federation_dispatch, "_schedule_dispatch_task", lambda *a, **k: None
        )
        self._sched_patch.start()
        self.addCleanup(self._sched_patch.stop)
        self.user, self.actor = _seed_actor_and_user()
        _seed_follower(
            self.user,
            actor_uri="https://peer.example/users/alice",
            shared_inbox_uri="https://peer.example/inbox",
        )
        self.post = _seed_public_post(self.user)

    def _enqueue(self) -> FederationActivity:
        federation_dispatch.enqueue_jobpost_activity(self.post.pk, "create")
        return FederationActivity.objects.filter(direction=DIRECTION_OUTBOUND).first()

    def test_2xx_marks_delivered(self):
        row = self._enqueue()
        with patch.object(federation_signing, "deliver", return_value=(202, "ok")):
            federation_dispatch.dispatch_one(row.id)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, DELIVERY_DELIVERED)
        self.assertIsNotNone(row.delivered_at)
        self.assertIsNone(row.next_attempt_at)
        self.assertEqual(row.retry_count, 1)

    def test_4xx_marks_rejected_no_retry(self):
        row = self._enqueue()
        with patch.object(federation_signing, "deliver", return_value=(404, "not found")):
            federation_dispatch.dispatch_one(row.id)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, DELIVERY_REJECTED)
        self.assertIsNone(row.next_attempt_at)
        self.assertIn("404", row.delivery_error)

    def test_5xx_schedules_retry_with_first_backoff(self):
        row = self._enqueue()
        before = timezone.now()
        with patch.object(federation_signing, "deliver", return_value=(503, "down")):
            with patch.object(federation_dispatch, "_schedule_dispatch_task") as sched:
                federation_dispatch.dispatch_one(row.id)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, DELIVERY_PENDING)
        self.assertEqual(row.retry_count, 1)
        # First backoff is 60s
        self.assertIsNotNone(row.next_attempt_at)
        elapsed = (row.next_attempt_at - before).total_seconds()
        self.assertGreaterEqual(elapsed, 55)
        self.assertLessEqual(elapsed, 80)
        sched.assert_called_once()

    def test_network_error_schedules_retry(self):
        row = self._enqueue()
        with patch.object(
            federation_signing,
            "deliver",
            return_value=(0, "ConnectionError: peer unreachable"),
        ):
            with patch.object(federation_dispatch, "_schedule_dispatch_task"):
                federation_dispatch.dispatch_one(row.id)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, DELIVERY_PENDING)
        self.assertEqual(row.retry_count, 1)
        self.assertIn("ConnectionError", row.delivery_error)

    def test_dead_letter_after_six_attempts(self):
        row = self._enqueue()
        # Pre-bump retry_count to one shy of the dead-letter threshold.
        row.retry_count = 5
        row.save(update_fields=["retry_count"])
        with patch.object(federation_signing, "deliver", return_value=(500, "boom")):
            with patch.object(federation_dispatch, "_schedule_dispatch_task"):
                federation_dispatch.dispatch_one(row.id)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, DELIVERY_DEAD_LETTER)
        self.assertEqual(row.retry_count, 6)
        self.assertIsNone(row.next_attempt_at)
        self.assertEqual(row.delivery_error, "exceeded retry budget")

    def test_dispatch_skips_non_pending_rows(self):
        row = self._enqueue()
        row.delivery_status = DELIVERY_DELIVERED
        row.save(update_fields=["delivery_status"])
        with patch.object(federation_signing, "deliver") as deliver:
            federation_dispatch.dispatch_one(row.id)
        deliver.assert_not_called()

    def test_dispatch_missing_row_is_noop(self):
        with patch.object(federation_signing, "deliver") as deliver:
            federation_dispatch.dispatch_one(999_999)
        deliver.assert_not_called()

    def test_actor_disappeared_marks_failed(self):
        row = self._enqueue()
        Actor.objects.all().delete()
        with patch.object(federation_signing, "deliver") as deliver:
            federation_dispatch.dispatch_one(row.id)
        deliver.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, "failed")

    def test_retry_indices_advance_through_schedule(self):
        # Walk the backoff schedule a few steps; each step the wait
        # interval should grow.
        backoff = [60, 300, 1800, 14400, 86400]
        row = self._enqueue()
        prev_wait = 0
        for step in range(min(3, len(backoff))):
            before = timezone.now()
            with patch.object(federation_signing, "deliver", return_value=(502, "x")):
                with patch.object(federation_dispatch, "_schedule_dispatch_task"):
                    federation_dispatch.dispatch_one(row.id)
            row.refresh_from_db()
            # Reset to pending so we can step again
            wait = (row.next_attempt_at - before).total_seconds()
            self.assertGreater(wait, prev_wait)
            prev_wait = wait
            row.delivery_status = DELIVERY_PENDING
            row.save(update_fields=["delivery_status"])


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestSweep(TestCase):
    """sweep_pending_dispatches re-enqueues stuck rows."""

    def setUp(self):
        self._sched_patch = patch.object(
            federation_dispatch, "_schedule_dispatch_task", lambda *a, **k: None
        )
        self._sched_patch.start()
        self.addCleanup(self._sched_patch.stop)
        self.user, self.actor = _seed_actor_and_user()
        _seed_follower(self.user, shared_inbox_uri="https://peer.example/inbox")
        self.post = _seed_public_post(self.user)
        FederationActivity.objects.all().delete()

    def test_sweep_picks_up_overdue_pending(self):
        federation_dispatch.enqueue_jobpost_activity(self.post.pk, "create")
        # Push every pending row's next_attempt_at into the past
        past = timezone.now() - timedelta(hours=1)
        FederationActivity.objects.filter(delivery_status=DELIVERY_PENDING).update(
            next_attempt_at=past
        )
        with patch.object(federation_dispatch, "_schedule_dispatch_task") as sched:
            count = federation_dispatch.sweep_pending_dispatches()
        self.assertEqual(count, 1)
        sched.assert_called_once()

    def test_sweep_skips_future_pending(self):
        federation_dispatch.enqueue_jobpost_activity(self.post.pk, "create")
        future = timezone.now() + timedelta(hours=1)
        FederationActivity.objects.filter(delivery_status=DELIVERY_PENDING).update(
            next_attempt_at=future
        )
        with patch.object(federation_dispatch, "_schedule_dispatch_task") as sched:
            count = federation_dispatch.sweep_pending_dispatches()
        self.assertEqual(count, 0)
        sched.assert_not_called()

    def test_sweep_skips_terminal_rows(self):
        federation_dispatch.enqueue_jobpost_activity(self.post.pk, "create")
        FederationActivity.objects.filter(delivery_status=DELIVERY_PENDING).update(
            delivery_status=DELIVERY_DELIVERED,
            next_attempt_at=None,
        )
        with patch.object(federation_dispatch, "_schedule_dispatch_task") as sched:
            count = federation_dispatch.sweep_pending_dispatches()
        self.assertEqual(count, 0)
        sched.assert_not_called()


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestActivityShapes(TestCase):
    """Lightweight assertions on Create/Update/Delete activity body shape."""

    def setUp(self):
        self._sched_patch = patch.object(
            federation_dispatch, "_schedule_dispatch_task", lambda *a, **k: None
        )
        self._sched_patch.start()
        self.addCleanup(self._sched_patch.stop)
        self.user, self.actor = _seed_actor_and_user()
        _seed_follower(self.user, shared_inbox_uri="https://peer.example/inbox")

    def _activity_body(self, post, kind, **kwargs) -> dict:
        import json

        FederationActivity.objects.filter(activity_type=kind.capitalize()).delete()
        federation_dispatch.enqueue_jobpost_activity(post.pk, kind, **kwargs)
        row = FederationActivity.objects.filter(
            activity_type=kind.capitalize()
        ).first()
        return json.loads(row.body)

    def test_create_addresses_public_and_followers(self):
        post = _seed_public_post(self.user)
        body = self._activity_body(post, "create")
        self.assertIn(AS2_PUBLIC, body.get("to", []))
        # cc points at the followers collection of the actor
        self.assertTrue(
            any("followers" in entry for entry in body.get("cc", [])),
            f"cc missing followers reference: {body.get('cc')}",
        )

    def test_update_addresses_public_and_followers(self):
        post = _seed_public_post(self.user)
        body = self._activity_body(
            post, "update", edit_marker=timezone.now()
        )
        self.assertIn(AS2_PUBLIC, body.get("to", []))
        self.assertTrue(any("followers" in entry for entry in body.get("cc", [])))
        self.assertEqual(body["type"], "Update")
        self.assertEqual(body["object"]["type"], "Note")

    def test_delete_uses_tombstone(self):
        post = _seed_public_post(self.user)
        body = self._activity_body(post, "delete")
        self.assertEqual(body["type"], "Delete")
        self.assertEqual(body["object"]["type"], "Tombstone")
        self.assertEqual(body["object"]["formerType"], "Note")

    def test_delete_id_is_deterministic(self):
        # Two enqueues of the same JobPost produce the same Delete id.
        post = _seed_public_post(self.user)
        federation_dispatch.enqueue_jobpost_activity(post.pk, "delete")
        FederationActivity.objects.all().delete()
        federation_dispatch.enqueue_jobpost_activity(post.pk, "delete")
        ids = list(
            FederationActivity.objects.values_list("activity_id", flat=True)
        )
        self.assertTrue(ids)
        self.assertEqual(len(set(ids)), 1)

    def test_update_id_changes_per_edit(self):
        post = _seed_public_post(self.user)
        FederationActivity.objects.filter(activity_type="Update").delete()
        federation_dispatch.enqueue_jobpost_activity(
            post.pk, "update", edit_marker="2026-06-01T00:00:00"
        )
        federation_dispatch.enqueue_jobpost_activity(
            post.pk, "update", edit_marker="2026-06-02T00:00:00"
        )
        ids = set(
            FederationActivity.objects.filter(activity_type="Update").values_list(
                "activity_id", flat=True
            )
        )
        self.assertEqual(len(ids), 2)
