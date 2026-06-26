"""Phase 5b ActivityPub outbox enumeration tests.

Pins the read-only outbox contract:

* metadata-only ``OrderedCollection`` (no ``page``) advertises
  ``totalItems`` + ``first`` / ``last`` (suppressed when empty)
* ``?page=N`` returns an ``OrderedCollectionPage`` with up to
  ``ACTIVITYPUB_OUTBOX_PAGE_SIZE`` ``Create(Note)`` activities,
  ``next`` / ``prev`` / ``partOf`` linkage
* private + wrong-actor + null-user posts excluded
* activity envelope shape — ``actor``, ``to`` (Public), ``cc``
  (followers), ``object.type == Note``, ``object.attributedTo``
* activity ``id`` is deterministic across two requests for the same
  JobPost (Phase 5b doesn't persist Activity rows — UUIDs derive
  from the JobPost id via UUID5)
* page=0, page=K+1, non-integer page → 404
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from job_hunting.models import Actor, JobPost
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


TEST_ORIGIN = "http://testserver"


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_OUTBOX_PAGE_SIZE=20)
class TestOutboxEmpty(TestCase):
    """Metadata-only outbox + 404 on stale page references when empty."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )

    def test_empty_outbox_metadata_omits_first_and_last(self):
        response = self.client.get("/actors/dough/outbox")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(body["type"], "OrderedCollection")
        self.assertEqual(body["totalItems"], 0)
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/actors/dough/outbox")
        self.assertNotIn("first", body)
        self.assertNotIn("last", body)
        self.assertNotIn("orderedItems", body)

    def test_empty_outbox_page_one_is_404(self):
        # Metadata suppresses ``first``; peers requesting page=1 anyway
        # get a 404 — the absence of advertised pages is the contract.
        response = self.client.get("/actors/dough/outbox?page=1")
        self.assertEqual(response.status_code, 404)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_OUTBOX_PAGE_SIZE=20)
class TestOutboxWithPosts(TestCase):
    """Single-page outbox with a mix of public, private, and other-user posts."""

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
        self.other_user_post = JobPost.objects.create(
            created_by=self.other,
            title="Someone else's",
            description="Belongs to other",
            link="https://example.com/jobs/3",
            audience=[AS2_PUBLIC],
        )

    def test_metadata_advertises_first_and_last_when_nonempty(self):
        response = self.client.get("/actors/dough/outbox")
        body = response.json()
        self.assertEqual(body["totalItems"], 1)
        self.assertEqual(body["first"], f"{TEST_ORIGIN}/actors/dough/outbox?page=1")
        self.assertEqual(body["last"], f"{TEST_ORIGIN}/actors/dough/outbox?page=1")

    def test_page_one_returns_ordered_collection_page(self):
        response = self.client.get("/actors/dough/outbox?page=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(body["type"], "OrderedCollectionPage")
        self.assertEqual(body["partOf"], f"{TEST_ORIGIN}/actors/dough/outbox")
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/actors/dough/outbox?page=1")
        self.assertEqual(len(body["orderedItems"]), 1)
        # Single page, so neither next nor prev should appear.
        self.assertNotIn("next", body)
        self.assertNotIn("prev", body)

    def test_private_post_not_included(self):
        response = self.client.get("/actors/dough/outbox?page=1")
        ids = [item["object"]["id"] for item in response.json()["orderedItems"]]
        self.assertNotIn(
            f"{TEST_ORIGIN}/job-posts/{self.private_post.id}", ids
        )

    def test_other_users_post_not_included(self):
        response = self.client.get("/actors/dough/outbox?page=1")
        ids = [item["object"]["id"] for item in response.json()["orderedItems"]]
        self.assertNotIn(
            f"{TEST_ORIGIN}/job-posts/{self.other_user_post.id}", ids
        )

    def test_activity_envelope_shape(self):
        response = self.client.get("/actors/dough/outbox?page=1")
        activity = response.json()["orderedItems"][0]
        actor_uri = f"{TEST_ORIGIN}/actors/dough"
        self.assertEqual(activity["type"], "Create")
        self.assertEqual(activity["actor"], actor_uri)
        self.assertEqual(activity["to"], [AS2_PUBLIC])
        self.assertEqual(activity["cc"], [f"{actor_uri}/followers"])
        self.assertTrue(activity["id"].startswith(f"{TEST_ORIGIN}/activities/"))

        note = activity["object"]
        self.assertEqual(note["type"], "Note")
        self.assertEqual(note["attributedTo"], actor_uri)
        self.assertEqual(
            note["id"], f"{TEST_ORIGIN}/job-posts/{self.public_post.id}"
        )
        self.assertEqual(note["to"], [AS2_PUBLIC])
        # BACK-97: the Note body is the lean line-composer (title header +
        # real-description hook + link + hashtags), not the bare
        # ``<p>{description}</p>`` echo. No AS2 ``summary`` (CW trap).
        self.assertIn("🟢 Senior Engineer", note["content"])
        self.assertIn("A great role", note["content"])
        self.assertNotIn("summary", note)
        self.assertEqual(note["url"], self.public_post.canonical_link or self.public_post.link)

    def test_activity_id_is_deterministic_across_requests(self):
        # Phase 5b doesn't persist Activity rows — UUID5 over the
        # JobPost id keeps the activity ``id`` stable so peers caching
        # by id see the same URI on every fetch.
        first = self.client.get("/actors/dough/outbox?page=1").json()
        second = self.client.get("/actors/dough/outbox?page=1").json()
        self.assertEqual(
            first["orderedItems"][0]["id"],
            second["orderedItems"][0]["id"],
        )

    def test_html_description_is_tag_stripped_in_hook(self):
        # BACK-97: the hook is plain text — embedded markup in the
        # description is stripped (and the composed line escaped) so a
        # JD's raw HTML can't break the Note envelope. The readable text
        # survives; the raw tags do not.
        self.public_post.description = "<p>Pre-rendered <strong>HTML</strong></p>"
        self.public_post.save()
        response = self.client.get("/actors/dough/outbox?page=1")
        note = response.json()["orderedItems"][0]["object"]
        self.assertIn("Pre-rendered", note["content"])
        self.assertIn("HTML", note["content"])
        self.assertNotIn("<strong>", note["content"])


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_OUTBOX_PAGE_SIZE=2)
class TestOutboxPagination(TestCase):
    """Multi-page outbox with the page size shrunk for testability."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        # 5 public posts → 3 pages at page_size=2.
        self.posts = [
            JobPost.objects.create(
                created_by=self.user,
                title=f"Role {i}",
                description=f"Description {i}",
                link=f"https://example.com/jobs/page-{i}",
                audience=[AS2_PUBLIC],
            )
            for i in range(5)
        ]

    def test_metadata_reflects_total_and_last_page(self):
        body = self.client.get("/actors/dough/outbox").json()
        self.assertEqual(body["totalItems"], 5)
        self.assertEqual(body["last"], f"{TEST_ORIGIN}/actors/dough/outbox?page=3")

    def test_middle_page_has_next_and_prev(self):
        body = self.client.get("/actors/dough/outbox?page=2").json()
        self.assertEqual(len(body["orderedItems"]), 2)
        self.assertEqual(body["next"], f"{TEST_ORIGIN}/actors/dough/outbox?page=3")
        self.assertEqual(body["prev"], f"{TEST_ORIGIN}/actors/dough/outbox?page=1")

    def test_last_page_has_prev_but_no_next(self):
        body = self.client.get("/actors/dough/outbox?page=3").json()
        # 5 items at page_size=2 → page 3 has the remainder (1 item).
        self.assertEqual(len(body["orderedItems"]), 1)
        self.assertNotIn("next", body)
        self.assertEqual(body["prev"], f"{TEST_ORIGIN}/actors/dough/outbox?page=2")

    def test_page_zero_is_404(self):
        response = self.client.get("/actors/dough/outbox?page=0")
        self.assertEqual(response.status_code, 404)

    def test_page_past_last_is_404(self):
        response = self.client.get("/actors/dough/outbox?page=99")
        self.assertEqual(response.status_code, 404)

    def test_non_integer_page_is_404(self):
        response = self.client.get("/actors/dough/outbox?page=abc")
        self.assertEqual(response.status_code, 404)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestOutboxActorWithoutUser(TestCase):
    """Future instance-actor case: ``actor.user_id is None`` → empty outbox."""

    def setUp(self):
        # No `user=` link — models the future Application/Service actor
        # representing the instance itself rather than a real human.
        self.actor = Actor.objects.create(
            preferred_username="instance", type="Application", user=None,
        )

    def test_user_less_actor_returns_empty_collection(self):
        response = self.client.get("/actors/instance/outbox")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totalItems"], 0)
        self.assertNotIn("first", body)

    def test_user_less_actor_page_one_is_404(self):
        response = self.client.get("/actors/instance/outbox?page=1")
        self.assertEqual(response.status_code, 404)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestOutboxUnknownActor(TestCase):
    """Unknown handle → AS2-shaped 404, not Django's HTML template."""

    def test_unknown_actor_outbox_returns_as2_shaped_404(self):
        response = self.client.get("/actors/nobody/outbox")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/activity+json")
