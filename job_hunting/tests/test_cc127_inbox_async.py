"""CC-127 — inbox accept-then-async + bounded/negatively-cached key fetch.

Covers what the pre-CC-127 synchronous inbox suite can't:

* the edge returns 202 and ENQUEUES verify+process — no key fetch on the
  web thread (the defect: a slow/dead peer's key endpoint pinned workers)
* cheap edge rejects (unsigned / oversized / malformed / unknown-actor
  self-Delete / stale / rate-limited) never enqueue and never fetch
* the Mastodon ``skip_unknown_actor_activity`` pre-drop
* ``fetch_actor_public_key``: split connect/read timeout, redirect cap,
  and NEGATIVE caching of failures (the storm relief valve)
* ``process_inbound_activity`` (the worker): verify+dispatch on success,
  cheap drop (no raise, no side effect) on unverifiable

Runs with ``ACTIVITYPUB_INBOX_DISPATCH_SYNC=False`` (prod async mode) and
asserts on the ``async_task`` enqueue, then drives the worker explicitly —
mirroring the phase5d dispatch tests that call ``dispatch_one`` directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from job_hunting.lib import federation_inbox, federation_signing
from job_hunting.lib.federation_signing import (
    SignatureVerificationError,
    compute_digest_header,
)
from job_hunting.models import Actor, FederationActivity, FederationFollower
from job_hunting.tests.test_activitypub_phase5c_inbox import (
    _create_note_body,
    _follow_body,
    _patch_deliver_ok,
    _patch_peer_actor_endpoints,
    _patch_peer_key,
    _sign_inbox,
)
from job_hunting.tests.test_activitypub_phase5c_signing import FakeRemoteActor


User = get_user_model()

TEST_ORIGIN = "http://testserver"
TASK_PATH = "job_hunting.lib.federation_inbox.process_inbound_activity"


def _meta(headers: dict[str, str]) -> dict[str, str]:
    return {
        f"HTTP_{k.upper().replace('-', '_')}": v
        for k, v in headers.items()
        if k.lower() != "host"
    }


def _delete_actor_body(peer: FakeRemoteActor) -> bytes:
    """A self-referential Delete (actor == object) — the account-deletion
    shape a suspended fediverse account broadcasts."""
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{peer.actor_uri}#delete",
        "type": "Delete",
        "actor": peer.actor_uri,
        "object": peer.actor_uri,
    }
    return json.dumps(activity).encode("utf-8")


def _update_actor_body(peer: FakeRemoteActor) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{peer.actor_uri}#update-1",
        "type": "Update",
        "actor": peer.actor_uri,
        "object": {"id": peer.actor_uri, "type": "Person"},
    }
    return json.dumps(activity).encode("utf-8")


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_INBOX_DISPATCH_SYNC=False)
class TestInboxEdgeEnqueues(TestCase):
    """The edge 202s + enqueues, and never fetches a key on the web thread."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"

    def _post(self, body, headers):
        return self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            HTTP_HOST="testserver", **_meta(headers),
        )

    def test_valid_signed_post_202_enqueues_and_does_not_fetch(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = _sign_inbox(self.peer, self.path, body)
        with patch("django_q.tasks.async_task") as mock_async, \
                patch.object(federation_signing, "fetch_actor_public_key") as mock_fetch:
            response = self._post(body, headers)
        self.assertEqual(response.status_code, 202)
        # Enqueued exactly once, with the raw request the worker re-verifies.
        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], TASK_PATH)
        self.assertEqual(kwargs["actor_kind"], "person")
        self.assertEqual(kwargs["identifier"], "dough")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["path"], self.path)
        self.assertEqual(kwargs["body"], body)
        self.assertIn("Signature", kwargs["headers"])
        # The whole point: NO key fetch on the web thread, no side effect.
        mock_fetch.assert_not_called()
        self.assertEqual(FederationFollower.objects.count(), 0)
        self.assertEqual(FederationActivity.objects.count(), 0)

    def test_unsigned_post_401_no_enqueue(self):
        body = _follow_body(self.peer, self.target_uri)
        with patch("django_q.tasks.async_task") as mock_async:
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                HTTP_DATE=format_datetime(datetime.now(tz=timezone.utc), usegmt=True),
                HTTP_DIGEST=compute_digest_header(body),
            )
        self.assertEqual(response.status_code, 401)
        mock_async.assert_not_called()

    def test_stale_date_401_no_enqueue(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = _sign_inbox(self.peer, self.path, body)
        headers["Date"] = format_datetime(
            datetime(2020, 1, 1, tzinfo=timezone.utc), usegmt=True
        )
        with patch("django_q.tasks.async_task") as mock_async:
            response = self._post(body, headers)
        self.assertEqual(response.status_code, 401)
        mock_async.assert_not_called()

    def test_malformed_json_400_no_enqueue(self):
        with patch("django_q.tasks.async_task") as mock_async:
            response = self.client.post(
                self.path, data=b"{not json",
                content_type="application/activity+json",
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 400)
        mock_async.assert_not_called()

    def test_actor_not_found_404_no_enqueue(self):
        with patch("django_q.tasks.async_task") as mock_async:
            response = self.client.post(
                "/actors/ghost/inbox", data=b"{}",
                content_type="application/activity+json",
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 404)
        mock_async.assert_not_called()

    @override_settings(ACTIVITYPUB_BODY_MAX_BYTES=128)
    def test_body_too_large_400_no_enqueue(self):
        with patch("django_q.tasks.async_task") as mock_async:
            response = self.client.post(
                self.path, data=b"x" * 256,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
            )
        self.assertEqual(response.status_code, 400)
        mock_async.assert_not_called()

    @override_settings(ACTIVITYPUB_INBOX_RATE_LIMIT_PER_HOUR=1)
    def test_rate_limited_429_does_not_enqueue_the_throttled_request(self):
        with patch("django_q.tasks.async_task") as mock_async:
            b1 = _follow_body(self.peer, self.target_uri, activity_id="rl-a")
            r1 = self._post(b1, _sign_inbox(self.peer, self.path, b1))
            b2 = _follow_body(self.peer, self.target_uri, activity_id="rl-b")
            r2 = self._post(b2, _sign_inbox(self.peer, self.path, b2))
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 429)
        # Only the first (accepted) request enqueued; the throttled one did not.
        self.assertEqual(mock_async.call_count, 1)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_INBOX_DISPATCH_SYNC=False)
class TestUnknownActorSelfActivityDrop(TestCase):
    """Mastodon skip_unknown_actor_activity: self-Delete/Update from an
    unknown actor is dropped BEFORE verify — no fetch, no queue slot."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()

    def _post(self, body, headers=None):
        extra = _meta(headers) if headers else {}
        return self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            HTTP_HOST="testserver", **extra,
        )

    def test_unknown_actor_self_delete_dropped_before_fetch(self):
        body = _delete_actor_body(self.peer)  # unsigned — drop is pre-verify
        with patch("django_q.tasks.async_task") as mock_async, \
                patch.object(federation_signing, "fetch_actor_public_key") as mock_fetch:
            response = self._post(body)
        self.assertEqual(response.status_code, 202)
        mock_async.assert_not_called()
        mock_fetch.assert_not_called()

    def test_unknown_actor_self_update_dropped(self):
        body = _update_actor_body(self.peer)
        with patch("django_q.tasks.async_task") as mock_async:
            response = self._post(body)
        self.assertEqual(response.status_code, 202)
        mock_async.assert_not_called()

    def test_known_actor_self_delete_falls_through_to_enqueue(self):
        # Prior interaction makes the actor "known" → not dropped.
        FederationFollower.objects.create(
            local_user=self.user,
            actor_uri=self.peer.actor_uri,
            inbox_uri=f"{self.peer.actor_uri}/inbox",
            instance_host="peer.example",
        )
        body = _delete_actor_body(self.peer)
        headers = _sign_inbox(self.peer, self.path, body)
        with patch("django_q.tasks.async_task") as mock_async:
            response = self._post(body, headers)
        self.assertEqual(response.status_code, 202)
        mock_async.assert_called_once()

    def test_normal_delete_not_dropped(self):
        # actor != object (a real job-post Delete) → not a self-activity.
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{self.peer.actor_uri}/activities/del-jp",
            "type": "Delete",
            "actor": self.peer.actor_uri,
            "object": f"{TEST_ORIGIN}/job-posts/AbCd123456",
        }
        body = json.dumps(activity).encode("utf-8")
        headers = _sign_inbox(self.peer, self.path, body)
        with patch("django_q.tasks.async_task") as mock_async:
            response = self._post(body, headers)
        self.assertEqual(response.status_code, 202)
        mock_async.assert_called_once()


def _fake_actor_response(status_code: int, pem: str | None = None):
    """Minimal stand-in for a streamed requests response."""
    if pem is not None:
        payload = json.dumps({"publicKey": {"publicKeyPem": pem}}).encode("utf-8")
    else:
        payload = b""
    raw = SimpleNamespace(read=lambda n, decode_content=True: payload)
    return SimpleNamespace(status_code=status_code, raw=raw, close=lambda: None)


class TestKeyFetchBounded(TestCase):
    """fetch_actor_public_key — split timeout, redirect cap, negative cache."""

    def setUp(self):
        cache.clear()
        self.uri = "https://peer.example/users/alice"
        self.peer = FakeRemoteActor(self.uri)

    @override_settings(
        ACTIVITYPUB_PEER_KEY_FETCH_CONNECT_TIMEOUT=2,
        ACTIVITYPUB_PEER_KEY_FETCH_READ_TIMEOUT=7,
    )
    def test_uses_split_connect_read_timeout(self):
        with patch("requests.Session.get") as mock_get:
            mock_get.return_value = _fake_actor_response(200, self.peer.public_pem)
            pem = federation_signing.fetch_actor_public_key(self.uri)
        self.assertEqual(pem, self.peer.public_pem)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["timeout"], (2.0, 7.0))

    def test_timeout_raises_unreachable_and_negative_caches(self):
        with patch(
            "requests.Session.get",
            side_effect=requests.exceptions.ConnectTimeout("boom"),
        ) as mock_get:
            with self.assertRaises(SignatureVerificationError) as c1:
                federation_signing.fetch_actor_public_key(self.uri)
            self.assertEqual(c1.exception.verdict, "peer_unreachable")
            # Second attempt short-circuits on the negative cache — no refetch.
            with self.assertRaises(SignatureVerificationError) as c2:
                federation_signing.fetch_actor_public_key(self.uri)
            self.assertEqual(c2.exception.verdict, "peer_unreachable")
            self.assertEqual(mock_get.call_count, 1)

    def test_too_many_redirects_treated_as_unreachable(self):
        with patch(
            "requests.Session.get",
            side_effect=requests.exceptions.TooManyRedirects("loop"),
        ):
            with self.assertRaises(SignatureVerificationError) as ctx:
                federation_signing.fetch_actor_public_key(self.uri)
        self.assertEqual(ctx.exception.verdict, "peer_unreachable")

    def test_404_raises_fetch_failed_and_negative_caches(self):
        with patch("requests.Session.get") as mock_get:
            mock_get.return_value = _fake_actor_response(404)
            with self.assertRaises(SignatureVerificationError) as ctx:
                federation_signing.fetch_actor_public_key(self.uri)
            self.assertEqual(ctx.exception.verdict, "peer_actor_fetch_failed")
            with self.assertRaises(SignatureVerificationError):
                federation_signing.fetch_actor_public_key(self.uri)
            self.assertEqual(mock_get.call_count, 1)  # negative-cached

    def test_positive_cache_hit_skips_network(self):
        cache.set(f"ap:pubkey:{self.uri}", self.peer.public_pem, 300)
        with patch("requests.Session.get") as mock_get:
            pem = federation_signing.fetch_actor_public_key(self.uri)
        self.assertEqual(pem, self.peer.public_pem)
        mock_get.assert_not_called()


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestWorkerVerifyDispatch(TestCase):
    """process_inbound_activity — verify+dispatch on success, cheap drop else."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/actors/dough"

    def _run(self, body, headers, identifier="dough"):
        federation_inbox.process_inbound_activity(
            actor_kind="person", identifier=identifier,
            method="POST", path=self.path, headers=headers, body=body,
        )

    def test_valid_follow_creates_follower_and_accept(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = self.peer.sign_request("POST", self.path, body, "testserver")
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            self._run(body, headers)
        self.assertTrue(
            FederationFollower.objects.filter(actor_uri=self.peer.actor_uri).exists()
        )
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Follow"
            ).exists()
        )
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="outbound", activity_type="Accept"
            ).exists()
        )

    def test_wrong_key_drops_no_side_effect_no_raise(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = self.peer.sign_request("POST", self.path, body, "testserver")
        wrong = FakeRemoteActor(self.peer.actor_uri)
        with patch.object(
            federation_signing, "fetch_actor_public_key",
            return_value=wrong.public_pem,
        ):
            self._run(body, headers)  # must not raise
        self.assertEqual(FederationFollower.objects.count(), 0)
        self.assertFalse(FederationActivity.objects.filter(direction="inbound").exists())

    def test_unreachable_peer_drops_cleanly(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = self.peer.sign_request("POST", self.path, body, "testserver")
        with patch.object(
            federation_signing, "fetch_actor_public_key",
            side_effect=SignatureVerificationError("peer_unreachable"),
        ):
            self._run(body, headers)  # must not raise
        self.assertEqual(FederationFollower.objects.count(), 0)

    def test_missing_actor_is_noop(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = self.peer.sign_request("POST", self.path, body, "testserver")
        with _patch_peer_key(self.peer):
            self._run(body, headers, identifier="ghost")  # must not raise
        self.assertEqual(FederationFollower.objects.count(), 0)

    def test_malformed_body_is_noop(self):
        self._run(b"{not json", {})  # must not raise
        self.assertEqual(FederationActivity.objects.count(), 0)

    def test_create_note_logs_audit_row(self):
        body = _create_note_body(self.peer)
        headers = self.peer.sign_request("POST", self.path, body, "testserver")
        with _patch_peer_key(self.peer):
            self._run(body, headers)
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Create"
            ).exists()
        )


class TestEnqueueSyncKnob(TestCase):
    """ACTIVITYPUB_INBOX_DISPATCH_SYNC routes in-band vs. async_task."""

    @override_settings(ACTIVITYPUB_INBOX_DISPATCH_SYNC=True)
    def test_sync_runs_worker_in_band(self):
        with patch.object(federation_inbox, "process_inbound_activity") as mock_proc:
            federation_inbox.enqueue_inbound_activity(
                actor_kind="person", identifier="dough",
                method="POST", path="/actors/dough/inbox", headers={}, body=b"{}",
            )
        mock_proc.assert_called_once()

    @override_settings(ACTIVITYPUB_INBOX_DISPATCH_SYNC=False)
    def test_async_schedules_task_and_does_not_run_in_band(self):
        with patch("django_q.tasks.async_task") as mock_async, \
                patch.object(federation_inbox, "process_inbound_activity") as mock_proc:
            federation_inbox.enqueue_inbound_activity(
                actor_kind="person", identifier="dough",
                method="POST", path="/actors/dough/inbox", headers={}, body=b"{}",
            )
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][0], TASK_PATH)
        mock_proc.assert_not_called()
