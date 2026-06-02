"""Phase 5a hot-patch — empty OrderedCollection stubs for outbox /
followers / following.

These exist purely so AP peers (Mastodon especially) enumerating the
Actor JSON don't dereference the three collection URIs and see Django's
HTML 404 template, which strict peers treat as a broken endpoint and
use to drop the actor from federation.

Real implementations land in Phase 5b (outbox enumeration of public
Create(Note) items, with pagination) and Phase 5c (followers /
following tracked by a real FederationFollower table). The contracts
pinned here are minimal: shape, content-type, 200/404 behaviour, and
``id`` consistency with the requested URL.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from job_hunting.models import Actor


User = get_user_model()


TEST_ORIGIN = "http://testserver"


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestActorCollectionStubs(TestCase):
    """Empty OrderedCollection responses for the three Actor sub-URIs."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough",
            type="Person",
            user=self.user,
        )

    # --- Outbox ---------------------------------------------------------

    def test_outbox_returns_ordered_collection_metadata(self):
        # Phase 5b replaced the empty-stub with metadata-only OrderedCollection:
        # totalItems advertises the public-Create count; ``orderedItems`` is
        # only present on OrderedCollectionPage responses (``?page=N``).
        # ``first`` / ``last`` are suppressed when the collection is empty so
        # peers don't dereference a phantom page.
        response = self.client.get("/actors/dough/outbox")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(
            body["@context"], "https://www.w3.org/ns/activitystreams"
        )
        self.assertEqual(body["type"], "OrderedCollection")
        self.assertEqual(body["totalItems"], 0)
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/actors/dough/outbox")

    def test_outbox_unknown_actor_returns_as2_shaped_404(self):
        response = self.client.get("/actors/nobody/outbox")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/activity+json")

    # --- Followers ------------------------------------------------------

    def test_followers_returns_empty_ordered_collection(self):
        response = self.client.get("/actors/dough/followers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(body["type"], "OrderedCollection")
        self.assertEqual(body["totalItems"], 0)
        self.assertEqual(body["orderedItems"], [])
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/actors/dough/followers")

    def test_followers_unknown_actor_returns_as2_shaped_404(self):
        response = self.client.get("/actors/nobody/followers")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/activity+json")

    # --- Following ------------------------------------------------------

    def test_following_returns_empty_ordered_collection(self):
        response = self.client.get("/actors/dough/following")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(body["type"], "OrderedCollection")
        self.assertEqual(body["totalItems"], 0)
        self.assertEqual(body["orderedItems"], [])
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/actors/dough/following")

    def test_following_unknown_actor_returns_as2_shaped_404(self):
        response = self.client.get("/actors/nobody/following")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/activity+json")

    # --- Actor JSON cross-reference -------------------------------------

    def test_collection_ids_match_actor_json_references(self):
        """The Actor JSON advertises these three URIs; the collection
        responses must echo back IDs that match what the peer
        dereferenced. If the Actor view changes the URI shape, this
        test fails loudly so we don't silently desync."""
        actor_response = self.client.get("/actors/dough/")
        actor_body = actor_response.json()

        for collection_name in ("outbox", "followers", "following"):
            advertised = actor_body[collection_name]
            collection_response = self.client.get(f"/actors/dough/{collection_name}")
            self.assertEqual(collection_response.status_code, 200)
            self.assertEqual(collection_response.json()["id"], advertised)
