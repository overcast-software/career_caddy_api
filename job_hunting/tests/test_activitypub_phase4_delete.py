"""Phase 4 inbox — `Delete` handler tombstones federated JobPost rows.

The contract pinned here:

* A signed inbound ``Delete`` whose object resolves to a known
  federated row, sent by the row's origin instance, sets
  ``source_deleted_at`` to the current time. The row itself is not
  deleted — local relationships (Score / JobApplication / CoverLetter)
  must survive remote-authority retractions.
* A signed inbound ``Delete`` whose object URI doesn't resolve to a
  local row is a no-op. The audit log row is still written; the
  response is 202 so the peer doesn't retry.
* A signed inbound ``Delete`` from a peer whose host doesn't match the
  target row's ``source_instance`` is a no-op. Cross-instance delete
  authority would let any signed peer wipe rows another instance
  originated, which is exactly the abuse vector the per-row origin
  pin exists to block.
* Re-delivery of the same activity_id is dropped upstream by replay
  protection; even when the replay protection is bypassed (different
  activity_id, same object), the second Delete preserves the first
  tombstone time. The audit story of "when did the origin first
  retract this" must not be overwritten.

The test harness reuses ``FakeRemoteActor`` from
``test_activitypub_phase5c_signing`` so the request signing path is
identical to Follow / Undo / Create. The only difference is the
activity body shape + the JobPost fixture each test seeds.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from job_hunting.lib import federation_signing
from job_hunting.models import Actor, FederationActivity, JobPost
from job_hunting.tests.test_activitypub_phase5c_signing import FakeRemoteActor


User = get_user_model()


TEST_ORIGIN = "http://testserver"
PEER_HOST = "peer.example"
PEER_ACTOR_URI = f"https://{PEER_HOST}/users/alice"


def _delete_body(peer: FakeRemoteActor, object_uri: str | dict,
                 activity_id: str | None = None) -> bytes:
    """Build a Delete envelope with either a bare URI or Tombstone dict.

    Mastodon emits the bare URI shape for Note retractions; some peers
    emit a Tombstone dict instead. Both must resolve through the same
    code path — the handler shouldn't care which shape the peer chose.
    """
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id or f"{peer.actor_uri}/activities/delete-1",
        "type": "Delete",
        "actor": peer.actor_uri,
        "object": object_uri,
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


def _post_signed(client, peer: FakeRemoteActor, path: str, body: bytes):
    headers = _sign_inbox(peer, path, body)
    return client.post(
        path, data=body,
        content_type="application/activity+json",
        **{f"HTTP_{k.upper().replace('-', '_')}": v
           for k, v in headers.items() if k.lower() != "host"},
        HTTP_HOST="testserver",
    )


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxDelete(TestCase):
    """Inbound `Delete` tombstones federated rows from their origin."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.path = "/actors/dough/inbox"
        self.peer = FakeRemoteActor(PEER_ACTOR_URI)

    def _seed_federated_jobpost(self, *, source_instance: str = PEER_HOST) -> JobPost:
        # Federated rows carry the remote origin host as
        # ``source_instance``; this matches what
        # federation_ingest.ingest_create_note writes when a Create(Note)
        # actually creates a JobPost. The object URI the peer will
        # reference in its Delete is derived from this row + the peer's
        # origin (see lib/as_object.object_uri).
        return JobPost.objects.create(
            title="Federated role",
            link=f"https://{source_instance}/jobs/abc",
            source="activitypub",
            source_instance=source_instance,
        )

    def _object_uri_for(self, job_post: JobPost,
                        instance: str = PEER_HOST) -> str:
        # Mirror lib/as_object.object_uri's shape (``{origin}/job-posts/{pk}``)
        # so the handler's URI-parse can resolve the JP. We hard-code the
        # shape here rather than importing object_uri because the handler
        # contract IS the URI shape — locking the test to a verbatim copy
        # surfaces regressions in either side instead of silently
        # tracking refactors that drift the contract.
        return f"https://{instance}/job-posts/{job_post.pk}"

    def test_delete_known_row_sets_source_deleted_at(self):
        jp = self._seed_federated_jobpost()
        object_uri = self._object_uri_for(jp)
        body = _delete_body(self.peer, object_uri)
        with _patch_peer_key(self.peer):
            response = _post_signed(self.client, self.peer, self.path, body)
        self.assertEqual(response.status_code, 202)
        jp.refresh_from_db()
        self.assertIsNotNone(jp.source_deleted_at)
        # Row itself survived — relationships are intact, only the
        # tombstone metadata flipped.
        self.assertTrue(JobPost.objects.filter(pk=jp.pk).exists())

    def test_delete_logs_inbound_activity(self):
        jp = self._seed_federated_jobpost()
        body = _delete_body(self.peer, self._object_uri_for(jp))
        with _patch_peer_key(self.peer):
            _post_signed(self.client, self.peer, self.path, body)
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Delete",
                actor_uri=self.peer.actor_uri,
            ).exists()
        )

    def test_delete_tombstone_dict_shape_works(self):
        # Some peers emit ``{ "id": "...", "type": "Tombstone" }`` rather
        # than the bare URI string. Both must resolve identically.
        jp = self._seed_federated_jobpost()
        tombstone = {
            "id": self._object_uri_for(jp),
            "type": "Tombstone",
        }
        body = _delete_body(self.peer, tombstone)
        with _patch_peer_key(self.peer):
            response = _post_signed(self.client, self.peer, self.path, body)
        self.assertEqual(response.status_code, 202)
        jp.refresh_from_db()
        self.assertIsNotNone(jp.source_deleted_at)

    def test_delete_unknown_uri_is_noop(self):
        # A Delete that names a URI we don't have a row for is a
        # silent 202 — federation peers routinely retract things we
        # never ingested.
        before = JobPost.objects.count()
        unknown_uri = f"https://{PEER_HOST}/job-posts/99999"
        body = _delete_body(self.peer, unknown_uri)
        with _patch_peer_key(self.peer):
            response = _post_signed(self.client, self.peer, self.path, body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(JobPost.objects.count(), before)
        # Audit row still written so 5e replay-style reports see it.
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Delete",
            ).exists()
        )

    def test_delete_from_wrong_instance_is_noop(self):
        # The signed sender's host is ``peer.example``; the row was
        # originated by ``other.example``. The host mismatch must
        # short-circuit the tombstone write — no cross-instance
        # delete authority.
        jp = self._seed_federated_jobpost(source_instance="other.example")
        # Object URI still points at our local PK, but the sender's host
        # doesn't match the row's source_instance.
        object_uri = self._object_uri_for(jp, instance=PEER_HOST)
        body = _delete_body(self.peer, object_uri)
        with _patch_peer_key(self.peer):
            response = _post_signed(self.client, self.peer, self.path, body)
        self.assertEqual(response.status_code, 202)
        jp.refresh_from_db()
        self.assertIsNone(jp.source_deleted_at)

    def test_redelivery_preserves_original_tombstone_time(self):
        # Replay protection drops the exact same activity_id, so we
        # exercise the idempotency guard with two distinct activity_ids
        # targeting the same object. The handler must preserve the
        # first-known source_deleted_at — overwriting would rewrite the
        # audit story of "when did the origin first retract this".
        jp = self._seed_federated_jobpost()
        object_uri = self._object_uri_for(jp)

        body1 = _delete_body(self.peer, object_uri, activity_id="del-1")
        with _patch_peer_key(self.peer):
            _post_signed(self.client, self.peer, self.path, body1)
        jp.refresh_from_db()
        first_tombstone = jp.source_deleted_at
        self.assertIsNotNone(first_tombstone)

        body2 = _delete_body(self.peer, object_uri, activity_id="del-2")
        with _patch_peer_key(self.peer):
            response = _post_signed(self.client, self.peer, self.path, body2)
        self.assertEqual(response.status_code, 202)
        jp.refresh_from_db()
        self.assertEqual(jp.source_deleted_at, first_tombstone)
