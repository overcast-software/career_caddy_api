"""Phase 6b — Company-actor inbox + Follow handshake tests.

Pins the wiring laid out in Phase 6b's company-actor follow-up:

- ``POST /companies/<slug>/inbox`` verifies HTTP Signatures (happy
  path + bad-sig reject) the same way the Phase 5c Person inbox does.
- ``Follow`` against a Company actor mints a ``FederationFollower`` row
  keyed off ``company`` (NOT ``local_user``), dispatches Accept(Follow)
  with the Company-actor URI as ``actor``, and surfaces the follower on
  ``/companies/<slug>/followers``.
- ``Undo(Follow)`` sets ``unfollowed_at`` on the matching company-keyed
  row.
- A subsequent inbound ``Create(Note)`` whose ``attributedTo`` matches
  the local Company actor URI creates a ``JobPostDiscovery`` for each
  active local follower of that Company.
- An inbound ``Create(Note)`` whose ``attributedTo`` doesn't match any
  local Company is a silent no-op — the JP still gets ingested but no
  discovery rows materialize (preserves Phase 5e visibility).

The signature math itself is covered by
``test_activitypub_phase5c_signing``; this module exercises the
Company-actor surface end-to-end via the Django test client.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from job_hunting.lib import federation_signing
from job_hunting.models import (
    Actor,
    Company,
    FederationActivity,
    FederationFollower,
    JobPost,
    JobPostDiscovery,
)
from job_hunting.models.actor import ACTOR_TYPE_ORGANIZATION
from job_hunting.models.job_post import AS2_PUBLIC
from job_hunting.tests.test_activitypub_phase5c_signing import FakeRemoteActor


User = get_user_model()


TEST_ORIGIN = "http://testserver"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _follow_body(peer: FakeRemoteActor, target_actor_uri: str,
                 activity_id: str | None = None) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id or f"{peer.actor_uri}/activities/cf-follow-1",
        "type": "Follow",
        "actor": peer.actor_uri,
        "object": target_actor_uri,
    }
    return json.dumps(activity).encode("utf-8")


def _undo_body(peer: FakeRemoteActor, target_actor_uri: str,
               activity_id: str | None = None) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id or f"{peer.actor_uri}/activities/cf-undo-1",
        "type": "Undo",
        "actor": peer.actor_uri,
        "object": {
            "type": "Follow",
            "actor": peer.actor_uri,
            "object": target_actor_uri,
        },
    }
    return json.dumps(activity).encode("utf-8")


def _create_note_body(
    peer: FakeRemoteActor,
    *,
    attributed_to: str,
    activity_id: str = "https://peer.example/activities/cf-create-1",
    note_url: str = "https://hire.example/jobs/cf-1",
    title: str = "Federated Engineer",
) -> bytes:
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id,
        "type": "Create",
        "actor": peer.actor_uri,
        "to": [AS2_PUBLIC],
        "object": {
            "id": f"{peer.actor_uri}/notes/1",
            "type": "Note",
            "name": title,
            "content": "<p>Cool federated role.</p>",
            "url": note_url,
            "published": "2026-05-01T12:00:00Z",
            "attributedTo": attributed_to,
        },
    }
    return json.dumps(activity).encode("utf-8")


def _sign(peer: FakeRemoteActor, path: str, body: bytes) -> dict[str, str]:
    return peer.sign_request("POST", path, body, "testserver")


def _meta(headers: dict[str, str]) -> dict[str, str]:
    return {
        f"HTTP_{k.upper().replace('-', '_')}": v
        for k, v in headers.items()
        if k.lower() != "host"
    }


def _patch_peer_key(peer: FakeRemoteActor):
    return patch.object(
        federation_signing,
        "fetch_actor_public_key",
        return_value=peer.public_pem,
    )


def _patch_peer_actor_endpoints(inbox: str | None = None,
                                shared: str | None = None):
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


# ---------------------------------------------------------------------------
# Follow → FederationFollower keyed off company + Accept dispatched
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestCompanyInboxFollow(TestCase):
    """Follow against /companies/<slug>/inbox lands a company-keyed row."""

    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Acme", slug="acme")
        self.path = "/companies/acme/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/companies/acme"

    def _follow(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = _sign(self.peer, self.path, body)
        return self.client.post(
            self.path, data=body,
            content_type="application/activity+json",
            HTTP_HOST="testserver",
            **_meta(headers),
        )

    def test_valid_follow_returns_202_and_creates_company_keyed_row(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            response = self._follow()
        self.assertEqual(response.status_code, 202)
        follower = FederationFollower.objects.get(
            company=self.company, actor_uri=self.peer.actor_uri,
        )
        self.assertIsNone(follower.local_user_id)
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
        self.assertIsNone(inbound.local_user_id)

    def test_valid_follow_logs_outbound_accept_with_company_actor(self):
        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                _patch_deliver_ok():
            self._follow()
        outbound = FederationActivity.objects.get(
            direction="outbound", activity_type="Accept",
        )
        self.assertEqual(outbound.delivery_status, "accepted")
        body = json.loads(outbound.body)
        self.assertEqual(body["type"], "Accept")
        # Accept's actor MUST be the Company actor URI, not the Person path.
        self.assertEqual(body["actor"], self.target_uri)
        self.assertEqual(body["object"]["type"], "Follow")

    def test_accept_signs_with_company_actor_uri(self):
        """The signing path uses the /companies/<slug> URI, not /actors/."""
        captured: list[dict] = []

        def fake_deliver(url, body, local_actor, *, timeout=None, actor_uri=None):
            captured.append({"url": url, "actor_uri": actor_uri})
            return 202, ""

        with _patch_peer_key(self.peer), \
                _patch_peer_actor_endpoints(), \
                patch.object(federation_signing, "deliver", side_effect=fake_deliver):
            response = self._follow()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["actor_uri"], self.target_uri)

    def test_follow_targeting_wrong_actor_returns_422(self):
        body = _follow_body(self.peer, f"{TEST_ORIGIN}/companies/someone-else")
        headers = _sign(self.peer, self.path, body)
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                **_meta(headers),
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(FederationFollower.objects.count(), 0)

    def test_unknown_company_returns_404(self):
        body = _follow_body(self.peer, f"{TEST_ORIGIN}/companies/nope")
        # No signature needed — slug check happens before signature verify.
        response = self.client.post(
            "/companies/nope/inbox", data=body,
            content_type="application/activity+json",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, 404)

    def test_bad_signature_rejected(self):
        body = _follow_body(self.peer, self.target_uri)
        headers = _sign(self.peer, self.path, body)
        # Verify against the wrong public key — same actor_uri, different key.
        other = FakeRemoteActor(self.peer.actor_uri)
        with patch.object(
            federation_signing, "fetch_actor_public_key",
            return_value=other.public_pem,
        ):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                **_meta(headers),
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(FederationFollower.objects.count(), 0)


# ---------------------------------------------------------------------------
# Undo(Follow) removes the company-keyed row
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestCompanyInboxUndo(TestCase):
    """Undo(Follow) → unfollowed_at on the company-keyed row."""

    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Acme", slug="acme")
        self.path = "/companies/acme/inbox"
        self.peer = FakeRemoteActor()
        self.target_uri = f"{TEST_ORIGIN}/companies/acme"
        self.follower = FederationFollower.objects.create(
            company=self.company,
            actor_uri=self.peer.actor_uri,
            inbox_uri="https://peer.example/users/alice/inbox",
            instance_host="peer.example",
            accepted_at=datetime.now(tz=timezone.utc),
        )

    def test_valid_undo_sets_unfollowed_at(self):
        body = _undo_body(self.peer, self.target_uri)
        headers = _sign(self.peer, self.path, body)
        with _patch_peer_key(self.peer):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                **_meta(headers),
            )
        self.assertEqual(response.status_code, 202)
        self.follower.refresh_from_db()
        self.assertIsNotNone(self.follower.unfollowed_at)

    def test_undo_with_no_matching_row_returns_404(self):
        other = FakeRemoteActor("https://peer.example/users/bob")
        body = _undo_body(other, self.target_uri)
        headers = _sign(other, self.path, body)
        with _patch_peer_key(other):
            response = self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                **_meta(headers),
            )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Followers collection enumerates company-keyed rows
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestCompanyFollowersCollection(TestCase):
    """The /companies/<slug>/followers collection surfaces the new rows."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme", slug="acme")

    def _seed(self, n: int):
        for i in range(n):
            FederationFollower.objects.create(
                company=self.company,
                actor_uri=f"https://peer.example/users/alice-{i}",
                inbox_uri=f"https://peer.example/users/alice-{i}/inbox",
                instance_host="peer.example",
                accepted_at=datetime.now(tz=timezone.utc),
            )

    def test_empty_followers_metadata(self):
        response = self.client.get("/companies/acme/followers")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totalItems"], 0)
        self.assertNotIn("first", body)

    def test_active_followers_listed(self):
        self._seed(2)
        FederationFollower.objects.create(
            company=self.company,
            actor_uri="https://peer.example/users/gone",
            inbox_uri="https://peer.example/users/gone/inbox",
            instance_host="peer.example",
            unfollowed_at=datetime.now(tz=timezone.utc),
        )
        response = self.client.get("/companies/acme/followers")
        body = response.json()
        self.assertEqual(body["totalItems"], 2)
        page1 = self.client.get("/companies/acme/followers?page=1").json()
        for item in page1["orderedItems"]:
            self.assertTrue(item.startswith("https://peer.example/users/alice-"))


# ---------------------------------------------------------------------------
# Inbound Create(Note) with matching attributedTo creates discovery
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestCompanyAttributedToDiscovery(TestCase):
    """A Create(Note) attributedTo a local Company actor materializes
    JobPostDiscovery rows for that Company's local followers."""

    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Acme", slug="acme")
        self.user = User.objects.create_user(username="viewer", password="pw")
        # A local user subscribing to the Company actor — both columns
        # set on a single row. The discovery helper resolves local
        # followers by company_id WHERE local_user_id IS NOT NULL, and
        # this row is exactly that shape.
        FederationFollower.objects.create(
            local_user=self.user,
            company=self.company,
            actor_uri=f"{TEST_ORIGIN}/actors/viewer",
            inbox_uri=f"{TEST_ORIGIN}/actors/viewer/inbox",
            instance_host="testserver",
            accepted_at=datetime.now(tz=timezone.utc),
        )

    def test_matching_attributed_to_creates_discoveries_for_local_followers(self):
        peer = FakeRemoteActor()
        path = "/companies/acme/inbox"
        body = _create_note_body(
            peer,
            attributed_to=f"{TEST_ORIGIN}/companies/acme",
            note_url="https://hire.example/jobs/attr-1",
        )
        headers = _sign(peer, path, body)
        with _patch_peer_key(peer):
            response = self.client.post(
                path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                **_meta(headers),
            )
        self.assertEqual(response.status_code, 202)
        jp = JobPost.objects.get(link="https://hire.example/jobs/attr-1")
        self.assertEqual(jp.source, "federation")
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post=jp, user=self.user, source="federation",
            ).exists()
        )

    def test_unmatched_company_silently_creates_no_discovery(self):
        # A Note attributed to a Company URI we don't know — the JP still
        # gets ingested (5e behavior), but no discovery rows are minted.
        peer = FakeRemoteActor()
        path = "/companies/acme/inbox"
        body = _create_note_body(
            peer,
            attributed_to=f"{TEST_ORIGIN}/companies/unknown-co",
            note_url="https://hire.example/jobs/attr-noop",
            activity_id="https://peer.example/activities/attr-noop",
        )
        headers = _sign(peer, path, body)
        with _patch_peer_key(peer):
            response = self.client.post(
                path, data=body,
                content_type="application/activity+json",
                HTTP_HOST="testserver",
                **_meta(headers),
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            JobPost.objects.filter(link="https://hire.example/jobs/attr-noop").exists()
        )
        self.assertEqual(
            JobPostDiscovery.objects.filter(source="federation").count(), 0
        )


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


class TestFollowerSchema(TestCase):
    """FederationFollower followee constraint accepts each shape:
    local_user only, company only, or both (a local user subscribing
    to a Company actor). Rejects ``(NULL, NULL)``."""

    def test_local_user_only_ok(self):
        user = User.objects.create_user(username="u1", password="pw")
        ff = FederationFollower.objects.create(
            local_user=user,
            actor_uri="https://peer.example/users/a",
            inbox_uri="https://peer.example/users/a/inbox",
            instance_host="peer.example",
        )
        self.assertIsNone(ff.company_id)

    def test_company_only_ok(self):
        company = Company.objects.create(name="Acme", slug="acme")
        ff = FederationFollower.objects.create(
            company=company,
            actor_uri="https://peer.example/users/b",
            inbox_uri="https://peer.example/users/b/inbox",
            instance_host="peer.example",
        )
        self.assertIsNone(ff.local_user_id)

    def test_both_set_ok_local_user_subscribing_to_company(self):
        """Phase 6b — a local user subscribing to a Company actor sets
        both columns. The discovery helper consumes this shape."""
        user = User.objects.create_user(username="u_sub", password="pw")
        company = Company.objects.create(name="SubCo", slug="sub-co")
        ff = FederationFollower.objects.create(
            local_user=user,
            company=company,
            actor_uri=f"{TEST_ORIGIN}/actors/u_sub",
            inbox_uri=f"{TEST_ORIGIN}/actors/u_sub/inbox",
            instance_host="testserver",
        )
        self.assertEqual(ff.local_user_id, user.id)
        self.assertEqual(ff.company_id, company.id)

    def test_both_null_rejected(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FederationFollower.objects.create(
                    local_user=None,
                    company=None,
                    actor_uri="https://peer.example/users/c",
                    inbox_uri="https://peer.example/users/c/inbox",
                    instance_host="peer.example",
                )

    def test_company_actor_lazy_created_on_first_inbox_hit(self):
        """A Company with no Actor row gets one lazily when first hit."""
        Company.objects.create(name="LazyCo", slug="lazy-co")
        self.assertFalse(Actor.objects.filter(preferred_username="lazy-co").exists())
        # GET on the Company actor with AS2 Accept triggers _ensure_company_actor.
        response = self.client.get(
            "/companies/lazy-co/",
            HTTP_ACCEPT="application/activity+json",
        )
        self.assertEqual(response.status_code, 200)
        actor = Actor.objects.get(preferred_username="lazy-co")
        self.assertEqual(actor.type, ACTOR_TYPE_ORGANIZATION)
