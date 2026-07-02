"""Phase 5c inbox + Follow + Followers collection tests.

Pins the inbox handler contract:

* Valid Follow → 202 + FederationFollower row + outbound Accept call
  (mocked) + audit log row
* Valid Undo(Follow) → 202 + unfollowed_at set
* Create(Note) → 202 + audit log row + NO JobPost created (5e's job)
* Unknown activity type → 202 + audit log Other
* CC-127 accept-then-async: cheap network-free failures (missing/stale
  Date, missing/mismatched Digest, unsigned) still 401 at the edge; the
  EXPENSIVE failures (wrong key / RSA mismatch) are deferred to the
  worker, so the edge 202s and the worker drops them.
* Body too large / malformed JSON → 400
* Follow targeting wrong actor / Undo with no matching follower → the
  edge 202s; the worker's handler rejects internally (no side effect).
* Replay protection — duplicate activity_id → silent 202
* Per-instance rate limit → 429
* Followers collection: real rows, paginated AS2 shape

The inbox verify+process runs in-band under TESTING
(ACTIVITYPUB_INBOX_DISPATCH_SYNC defaults True) so these side-effect
assertions hold synchronously; the async_task enqueue itself is covered
in test_cc127_inbox_async.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from job_hunting.lib import federation_signing
from job_hunting.lib.federation_signing import compute_digest_header
from job_hunting.models import (
    Actor,
    FederationActivity,
    FederationFollower,
    JobPost,
)
from job_hunting.tests.test_activitypub_phase5c_signing import FakeRemoteActor


User = get_user_model()


TEST_ORIGIN = "http://testserver"


def _follow_body(peer: FakeRemoteActor, target_actor_uri: str,
                 activity_id: str | None = None) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id or f"{peer.actor_uri}/activities/follow-1",
        "type": "Follow",
        "actor": peer.actor_uri,
        "object": target_actor_uri,
    }
    return json.dumps(activity).encode("utf-8")


def _undo_body(peer: FakeRemoteActor, target_actor_uri: str,
               activity_id: str | None = None) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id or f"{peer.actor_uri}/activities/undo-1",
        "type": "Undo",
        "actor": peer.actor_uri,
        "object": {
            "type": "Follow",
            "actor": peer.actor_uri,
            "object": target_actor_uri,
        },
    }
    return json.dumps(activity).encode("utf-8")


def _create_note_body(peer: FakeRemoteActor,
                      activity_id: str | None = None) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id or f"{peer.actor_uri}/activities/create-1",
        "type": "Create",
        "actor": peer.actor_uri,
        "object": {
            "id": f"{peer.actor_uri}/notes/1",
            "type": "Note",
            "content": "Hello federation",
        },
    }
    return json.dumps(activity).encode("utf-8")


def _other_body(peer: FakeRemoteActor, kind: str = "Like") -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{peer.actor_uri}/activities/like-1",
        "type": kind,
        "actor": peer.actor_uri,
        "object": "https://example/object",
    }
    return json.dumps(activity).encode("utf-8")


def _sign_inbox(peer: FakeRemoteActor, path: str, body: bytes,
                host: str = "testserver") -> dict[str, str]:
    return peer.sign_request("POST", path, body, host)


def _patch_peer_key(peer: FakeRemoteActor):
    return patch.object(
        federation_signing,
        "fetch_actor_public_key",
        return_value=peer.public_pem,
    )


def _patch_peer_actor_endpoints(inbox: str | None = None,
                                shared: str | None = None):
    """Skip the remote actor JSON-LD fetch in Follow dispatch."""
    from job_hunting.api.views import federation as fed_views
    return patch.object(
        fed_views,
        "_peer_actor_endpoints",
        return_value=(inbox or "https://peer.example/users/alice/inbox", shared),
    )


def _patch_deliver_ok():
    return patch.object(
        federation_signing,
        "deliver",
        return_value=(202, ""),
    )


def _patch_deliver_fail(status: int = 500, body: str = "boom"):
    return patch.object(
        federation_signing,
        "deliver",
        return_value=(status, body),
    )


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxFollow(TestCase):
    """Follow → FederationFollower + outbound Accept + audit log."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"

    def _follow(self, peer=None):
        peer = peer or self.peer
        body = _follow_body(peer, self.target_uri)
        headers = _sign_inbox(peer, self.path, body)
        return self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            **{f"HTTP_{k.upper().replace('-', '_')}": v
               for k, v in headers.items() if k.lower() != "host"},
            HTTP_HOST="testserver",
        )

    def test_valid_follow_returns_202_and_creates_row(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            response = self._follow()
        self.assertEqual(response.status_code, 202)
        follower = FederationFollower.objects.get(
            local_user=self.user, actor_uri=self.peer.actor_uri,
        )
        self.assertEqual(follower.inbox_uri, "https://peer.example/users/alice/inbox")
        self.assertIsNone(follower.unfollowed_at)
        self.assertIsNotNone(follower.accepted_at)

    def test_valid_follow_logs_inbound_activity(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            self._follow()
        inbound = FederationActivity.objects.get(
            direction="inbound", activity_type="Follow",
        )
        self.assertEqual(inbound.actor_uri, self.peer.actor_uri)
        self.assertEqual(inbound.target_uri, self.target_uri)
        self.assertEqual(inbound.local_user_id, self.user.id)
        self.assertIsNotNone(inbound.received_at)
        self.assertIsNotNone(inbound.signature_payload)

    def test_valid_follow_logs_outbound_accept(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            self._follow()
        outbound = FederationActivity.objects.get(
            direction="outbound", activity_type="Accept",
        )
        self.assertEqual(outbound.delivery_status, "accepted")
        self.assertIsNotNone(outbound.delivered_at)
        body = json.loads(outbound.body)
        self.assertEqual(body["type"], "Accept")
        self.assertEqual(body["actor"], self.target_uri)
        self.assertEqual(body["object"]["type"], "Follow")

    def test_accept_delivery_failure_sets_failed_status(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_fail(500, "internal error"):
            response = self._follow()
        # Inbox returns 202 to the original Follow regardless — delivery
        # failure of the Accept is OUR problem, not the peer's; 5d's
        # dispatcher will retry.
        self.assertEqual(response.status_code, 202)
        outbound = FederationActivity.objects.get(
            direction="outbound", activity_type="Accept",
        )
        self.assertEqual(outbound.delivery_status, "failed")
        self.assertIn("status=500", outbound.delivery_error)
        follower = FederationFollower.objects.get(actor_uri=self.peer.actor_uri)
        self.assertIsNone(follower.accepted_at)

    def test_follow_targeting_wrong_actor_returns_422(self):
        body = _follow_body(self.peer, f"{TEST_ORIGIN}/actors/someone-else")
        headers = _sign_inbox(self.peer, self.path, body)
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        # CC-127 accept-then-async: the edge 202s and enqueues; the worker
        # runs _handle_follow which rejects the target mismatch (no follower
        # created). The 422 is no longer surfaced to the peer.
        self.assertEqual(response.status_code, 202)
        self.assertEqual(FederationFollower.objects.count(), 0)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxUndo(TestCase):
    """Undo(Follow) → unfollowed_at on the FederationFollower row."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"
        # Seed an active follower row
        self.follower = FederationFollower.objects.create(
            local_user=self.user,
            actor_uri=self.peer.actor_uri,
            inbox_uri="https://peer.example/users/alice/inbox",
            instance_host="peer.example",
            accepted_at=datetime.now(tz=timezone.utc),
        )

    def _post(self, body):
        headers = _sign_inbox(self.peer, self.path, body)
        return self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            **{f"HTTP_{k.upper().replace('-', '_')}": v
               for k, v in headers.items() if k.lower() != "host"},
            HTTP_HOST="testserver",
        )

    def test_valid_undo_sets_unfollowed_at(self):
        body = _undo_body(self.peer, self.target_uri)
        with _patch_peer_key(self.peer):
            response = self._post(body)
        self.assertEqual(response.status_code, 202)
        self.follower.refresh_from_db()
        self.assertIsNotNone(self.follower.unfollowed_at)

    def test_undo_logs_audit_activity(self):
        body = _undo_body(self.peer, self.target_uri)
        with _patch_peer_key(self.peer):
            self._post(body)
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Undo",
                actor_uri=self.peer.actor_uri,
            ).exists()
        )

    def test_undo_with_no_matching_row_is_accepted(self):
        # Different peer, never followed. CC-127: the edge 202s; the worker
        # runs _handle_undo, finds no matching row, and no-ops (the 404 is
        # no longer surfaced to the peer under accept-then-async).
        other = FakeRemoteActor("https://peer.example/users/bob")
        body = _undo_body(other, self.target_uri)
        with _patch_peer_key(other):
            headers = _sign_inbox(other, self.path, body)
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 202)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxCreate(TestCase):
    """Create(Note) → audit log only; no JobPost in V1."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()

    def test_create_returns_202_and_logs(self):
        before_count = JobPost.objects.count()
        body = _create_note_body(self.peer)
        headers = _sign_inbox(self.peer, self.path, body)
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(JobPost.objects.count(), before_count)  # NO ingest
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Create",
            ).exists()
        )


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxForwardCompat(TestCase):
    """Unknown activity types log as Other and 202."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()

    def test_unknown_type_logs_other(self):
        body = _other_body(self.peer, kind="Like")
        headers = _sign_inbox(self.peer, self.path, body)
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Other",
            ).exists()
        )


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxSignatureFailures(TestCase):
    """Cheap pre-check failures → 401 at the edge; expensive → 202 + drop.

    CC-127 splits verification: the network-free pre-check (missing sig,
    stale Date, bad Digest) still 401s at the edge without a queue slot;
    the expensive remote-key-fetch + RSA leg (wrong key) is deferred to
    the worker, so a wrong-key mismatch is accepted (202) then dropped.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"

    def test_missing_signature_returns_401(self):
        body = _follow_body(self.peer, self.target_uri)
        # No signature headers at all
        response = self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            HTTP_HOST="testserver",
            HTTP_DATE=format_datetime(datetime.now(tz=timezone.utc), usegmt=True),
            HTTP_DIGEST=compute_digest_header(body),
        )
        self.assertEqual(response.status_code, 401)

    def test_stale_date_returns_401(self):
        body = _follow_body(self.peer, self.target_uri)
        # Date 30min in the past
        old = format_datetime(
            datetime(2020, 1, 1, tzinfo=timezone.utc), usegmt=True,
        )
        headers = _sign_inbox(self.peer, self.path, body)
        headers["Date"] = old  # untouched signature → both verdicts apply; stale_date hits first
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 401)

    def test_digest_mismatch_returns_401(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = _sign_inbox(self.peer, self.path, body)
        headers["Digest"] = "SHA-256=" + ("A" * 44)  # wrong digest
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 401)

    def test_wrong_key_accepted_then_dropped(self):
        # CC-127: a wrong-key RSA mismatch is an EXPENSIVE (post-fetch)
        # failure, so it's deferred to the worker — the edge 202s and the
        # worker drops it after verify (no follower, no audit row). This is
        # the accept-then-async "unverifiable is cheap + off the web thread"
        # path; the peer no longer gets a (retry-triggering) 401.
        body = _follow_body(self.peer, self.target_uri)
        headers = _sign_inbox(self.peer, self.path, body)
        other = FakeRemoteActor(self.peer.actor_uri)
        with patch.object(
            federation_signing, "fetch_actor_public_key",
            return_value=other.public_pem,
        ):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(FederationFollower.objects.count(), 0)
        self.assertFalse(
            FederationActivity.objects.filter(direction="inbound").exists()
        )


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_BODY_MAX_BYTES=128)
class TestInboxBodyLimits(TestCase):
    """Pre-signature body validation: too-large + malformed JSON."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )

    def test_body_too_large_returns_400(self):
        oversized = b"x" * 256
        response = self.client.post(
            "/actors/dough/inbox",
            data=oversized,
            content_type="application/activity+json",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_returns_400(self):
        response = self.client.post(
            "/actors/dough/inbox",
            data=b"{not json",
            content_type="application/activity+json",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 400)

    def test_actor_not_found_returns_404(self):
        response = self.client.post(
            "/actors/nonexistent/inbox",
            data=b"{}",
            content_type="application/activity+json",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 404)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxReplayProtection(TestCase):
    """Duplicate activity_id → silent 202 (drop)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"

    def test_duplicate_activity_id_is_dropped(self):
        body = _follow_body(self.peer, self.target_uri, activity_id="dup-1")
        headers = _sign_inbox(self.peer, self.path, body)
        meta = {f"HTTP_{k.upper().replace('-', '_')}": v
                for k, v in headers.items() if k.lower() != "host"}
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            r1 = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver", **meta,
            )
            # Re-sign so Date isn't stale on the replay
            headers2 = _sign_inbox(self.peer, self.path, body)
            meta2 = {f"HTTP_{k.upper().replace('-', '_')}": v
                     for k, v in headers2.items() if k.lower() != "host"}
            r2 = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver", **meta2,
            )
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 202)
        # Only one inbound row + one outbound Accept
        self.assertEqual(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Follow",
            ).count(),
            1,
        )
        self.assertEqual(
            FederationActivity.objects.filter(
                direction="outbound", activity_type="Accept",
            ).count(),
            1,
        )


@override_settings(
    INSTANCE_ORIGIN=TEST_ORIGIN,
    ACTIVITYPUB_INBOX_RATE_LIMIT_PER_HOUR=2,
)
class TestInboxRateLimit(TestCase):
    """Per-instance rate limit → 429 after threshold."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"

    def _post_with_unique_activity_id(self, i: int):
        body = _follow_body(self.peer, self.target_uri, activity_id=f"rl-{i}")
        headers = _sign_inbox(self.peer, self.path, body)
        meta = {f"HTTP_{k.upper().replace('-', '_')}": v
                for k, v in headers.items() if k.lower() != "host"}
        return self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            HTTP_HOST="testserver", **meta,
        )

    def test_rate_limit_kicks_in_after_threshold(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            r1 = self._post_with_unique_activity_id(1)
            r2 = self._post_with_unique_activity_id(2)
            r3 = self._post_with_unique_activity_id(3)
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 202)
        self.assertEqual(r3.status_code, 429)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestFollowersCollection(TestCase):
    """The followers collection enumerates real rows after Phase 5c."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )

    def _seed_followers(self, n: int):
        rows = []
        for i in range(n):
            rows.append(FederationFollower.objects.create(
                local_user=self.user,
                actor_uri=f"https://peer.example/users/alice-{i}",
                inbox_uri=f"https://peer.example/users/alice-{i}/inbox",
                instance_host="peer.example",
                accepted_at=datetime.now(tz=timezone.utc),
            ))
        return rows

    def test_empty_followers_metadata_no_pages(self):
        response = self.client.get("/actors/dough/followers")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totalItems"], 0)
        self.assertNotIn("first", body)

    def test_unfollowed_rows_excluded(self):
        active = self._seed_followers(2)
        FederationFollower.objects.create(
            local_user=self.user,
            actor_uri="https://peer.example/users/gone",
            inbox_uri="https://peer.example/users/gone/inbox",
            instance_host="peer.example",
            unfollowed_at=datetime.now(tz=timezone.utc),
        )
        response = self.client.get("/actors/dough/followers")
        body = response.json()
        self.assertEqual(body["totalItems"], len(active))

    def test_followers_page_returns_actor_uris(self):
        self._seed_followers(2)
        response = self.client.get("/actors/dough/followers?page=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "OrderedCollectionPage")
        self.assertEqual(len(body["orderedItems"]), 2)
        # Bare actor URI strings, not full activities
        for item in body["orderedItems"]:
            self.assertIsInstance(item, str)
            self.assertTrue(item.startswith("https://peer.example/users/alice-"))

    @override_settings(ACTIVITYPUB_OUTBOX_PAGE_SIZE=2)
    def test_followers_pagination(self):
        self._seed_followers(5)
        # 5 rows, page size 2 → 3 pages
        meta = self.client.get("/actors/dough/followers").json()
        self.assertEqual(meta["totalItems"], 5)
        self.assertEqual(meta["last"], f"{TEST_ORIGIN}/actors/dough/followers?page=3")
        p1 = self.client.get("/actors/dough/followers?page=1").json()
        self.assertEqual(len(p1["orderedItems"]), 2)
        self.assertIn("next", p1)
        self.assertNotIn("prev", p1)
        p3 = self.client.get("/actors/dough/followers?page=3").json()
        self.assertEqual(len(p3["orderedItems"]), 1)
        self.assertNotIn("next", p3)
        self.assertIn("prev", p3)
