"""BACK-93 — AP object dereferencing tests.

Every outbox / delivered ``Create`` advertises
``object.id = {origin}/job-posts/<pk>`` and
``id = {origin}/activities/<uuid>``. Before this surface existed those
URIs fell through the apex to the SPA and a remote instance dereferencing
a post object id got HTML it could not ingest → no posts rendered.

Pins:

* ``GET /job-posts/<pk>`` with ``Accept: application/activity+json`` →
  200 ``application/activity+json`` Note, ``@context`` + ``id`` +
  ``attributedTo`` + ``to`` (Public).
* The dereferenced Note's ``id`` + ``attributedTo`` match BYTE-FOR-BYTE
  what the actor outbox advertises for the same post — the actual bug
  class (a mismatch makes Mastodon reject the object).
* private (audience empty) / non-local (federated origin) / missing posts
  → 404 AS2 — never leak a private post just because the caller asked
  for AS2.
* browser default (non-AP Accept) → JSON:API stub, never the Note.
* ``GET /activities/<uuid>`` replays the persisted outbound activity body
  as AS2; unknown / inbound-only ids → 404 AS2 (not SPA HTML).
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from job_hunting.lib.as_object import build_create_activity_for_jobpost
from job_hunting.models import Actor, FederationActivity, JobPost
from job_hunting.models.federation_activity import (
    ACTIVITY_TYPE_CREATE,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
)
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()

TEST_ORIGIN = "http://testserver"
AS2_ACCEPT = "application/activity+json"


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, CAREER_CADDY_INSTANCE="testserver")
class TestJobPostObjectDeref(TestCase):
    """Content-negotiated ``/job-posts/<pk>`` object endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.public_post = JobPost.objects.create(
            created_by=self.user,
            title="Senior Engineer",
            description="A great role",
            link="https://example.com/jobs/1",
            audience=[AS2_PUBLIC],
        )
        self.private_post = JobPost.objects.create(
            created_by=self.user,
            title="Private notes",
            description="Not for federation",
            link="https://example.com/jobs/2",
            audience=[],
        )
        self.remote_post = JobPost.objects.create(
            created_by=self.user,
            title="Federated elsewhere",
            description="Owned by a peer",
            link="https://remote.example/jobs/9",
            audience=[AS2_PUBLIC],
            source_instance="remote.example",
        )

    def _get_as2(self, path):
        return self.client.get(path, HTTP_ACCEPT=AS2_ACCEPT)

    def test_public_post_serves_note(self):
        response = self._get_as2(f"/job-posts/{self.public_post.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], AS2_ACCEPT)
        body = response.json()
        self.assertIn("@context", body)
        self.assertEqual(body["type"], "Note")
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/job-posts/{self.public_post.id}")
        self.assertEqual(body["attributedTo"], f"{TEST_ORIGIN}/actors/dough")
        self.assertIn(AS2_PUBLIC, body["to"])
        # BACK-97: lean line-composer body (dough has no rich opt-in here),
        # not the bare description echo; never an AS2 ``summary``.
        self.assertIn("🟢 Senior Engineer", body["content"])
        self.assertIn("A great role", body["content"])
        self.assertNotIn("summary", body)
        self.assertEqual(body["url"], "https://example.com/jobs/1")

    def test_trailing_slash_also_serves_note(self):
        response = self._get_as2(f"/job-posts/{self.public_post.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], AS2_ACCEPT)

    def test_note_id_and_attributed_to_match_outbox(self):
        # The dereferenced object MUST be byte-identical to what the
        # outbox advertised, or the peer rejects it. Pull the Create the
        # outbox emits for the same post and compare the object's id +
        # attributedTo against the standalone Note.
        outbox = self.client.get("/actors/dough/outbox?page=1").json()
        advertised = next(
            item["object"]
            for item in outbox["orderedItems"]
            if item["object"]["id"].endswith(f"/job-posts/{self.public_post.id}")
        )
        note = self._get_as2(f"/job-posts/{self.public_post.id}").json()
        self.assertEqual(note["id"], advertised["id"])
        self.assertEqual(note["attributedTo"], advertised["attributedTo"])

    def test_private_post_not_dereferenceable(self):
        response = self._get_as2(f"/job-posts/{self.private_post.id}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], AS2_ACCEPT)

    def test_remote_origin_post_not_served(self):
        # We are not authoritative for a federated row's object id (it is
        # rooted on its source instance) — refuse to claim it as ours.
        response = self._get_as2(f"/job-posts/{self.remote_post.id}")
        self.assertEqual(response.status_code, 404)

    def test_missing_post_404_as2(self):
        response = self._get_as2("/job-posts/99999999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], AS2_ACCEPT)

    def test_browser_default_returns_jsonapi_stub_not_note(self):
        response = self.client.get(f"/job-posts/{self.public_post.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.api+json")
        body = response.json()
        self.assertEqual(body["data"]["type"], "job-post")
        self.assertEqual(body["data"]["id"], str(self.public_post.id))
        self.assertNotIn("@context", body)

    def test_browser_default_private_is_404(self):
        response = self.client.get(f"/job-posts/{self.private_post.id}")
        self.assertEqual(response.status_code, 404)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, CAREER_CADDY_INSTANCE="testserver")
class TestActivityDeref(TestCase):
    """``/activities/<uuid>`` replays persisted outbound activity bodies."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        self.post = JobPost.objects.create(
            created_by=self.user,
            title="Senior Engineer",
            description="A great role",
            link="https://example.com/jobs/1",
            audience=[AS2_PUBLIC],
        )
        self.activity = build_create_activity_for_jobpost(self.post, self.actor)
        self.activity_uuid = self.activity["id"].rsplit("/", 1)[-1]
        FederationActivity.objects.create(
            direction=DIRECTION_OUTBOUND,
            activity_type=ACTIVITY_TYPE_CREATE,
            activity_id=self.activity["id"],
            actor_uri=self.activity["actor"],
            target_uri="https://mstdn.social/users/peer/inbox",
            local_user=self.user,
            body=json.dumps(self.activity),
        )

    def _get_as2(self, path):
        return self.client.get(path, HTTP_ACCEPT=AS2_ACCEPT)

    def test_outbound_activity_replayed_as_as2(self):
        response = self._get_as2(f"/activities/{self.activity_uuid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], AS2_ACCEPT)
        body = response.json()
        self.assertEqual(body["type"], "Create")
        self.assertEqual(body["id"], self.activity["id"])
        self.assertEqual(
            body["object"]["id"], f"{TEST_ORIGIN}/job-posts/{self.post.id}"
        )

    def test_unknown_activity_uuid_404_as2(self):
        response = self._get_as2(
            "/activities/00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], AS2_ACCEPT)

    def test_inbound_activity_not_served(self):
        # An inbound row (authored by a peer) must never be replayed from
        # our /activities surface even if its id happens to sit under our
        # origin — only OUTBOUND rows are ours.
        inbound_uuid = "11111111-1111-1111-1111-111111111111"
        FederationActivity.objects.create(
            direction=DIRECTION_INBOUND,
            activity_type=ACTIVITY_TYPE_CREATE,
            activity_id=f"{TEST_ORIGIN}/activities/{inbound_uuid}",
            actor_uri="https://mstdn.social/users/peer",
            target_uri=f"{TEST_ORIGIN}/actors/dough",
            body=json.dumps({"type": "Create"}),
        )
        response = self._get_as2(f"/activities/{inbound_uuid}")
        self.assertEqual(response.status_code, 404)
