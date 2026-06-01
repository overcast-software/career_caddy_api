"""Phase 5a ActivityPub federation tests.

Pins the WebFinger + Actor view contract that Mastodon's first-contact
flow depends on, plus the lazy-keypair race-condition contract that
keeps duplicate keys out of the database under concurrent first-hit.

Scope (strict): 5a only — Actor model, WebFinger, Actor view, lazy
keypair, two mgmt commands. Outbox (5b), Inbox (5c), dispatch (5d),
ingest (5e) are out of scope.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from io import StringIO

from job_hunting.models import Actor


User = get_user_model()


TEST_ORIGIN = "http://testserver"


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestWebFinger(TestCase):
    """RFC 7033 WebFinger lookups for local actors.

    The harness assumes the server is reachable at ``testserver`` (Django
    test-client default host); INSTANCE_ORIGIN overrides keep the
    WebFinger ``acct:`` host check aligned with the request Host.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="dough",
            type="Person",
            user=self.user,
        )

    def test_webfinger_resolves_known_handle(self):
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "acct:dough@testserver"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/jrd+json")
        body = response.json()
        self.assertEqual(body["subject"], "acct:dough@testserver")
        self_link = next(
            link for link in body["links"] if link["rel"] == "self"
        )
        self.assertEqual(self_link["type"], "application/activity+json")
        self.assertEqual(self_link["href"], f"{TEST_ORIGIN}/actors/dough")

    def test_webfinger_unknown_handle_returns_404(self):
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "acct:nobody@testserver"},
        )
        self.assertEqual(response.status_code, 404)

    def test_webfinger_wrong_host_returns_404(self):
        # Refuse to advertise our actors under a foreign hostname —
        # otherwise we'd impersonate other instances during discovery.
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "acct:dough@evil.example.test"},
        )
        self.assertEqual(response.status_code, 404)

    def test_webfinger_malformed_resource_returns_400(self):
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "https://testserver/dough"},
        )
        self.assertEqual(response.status_code, 400)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestActorView(TestCase):
    """AS2 Actor JSON-LD, including lazy keypair on first hit."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="dough", password="pass", first_name="Danny", last_name="Noonan"
        )
        self.actor = Actor.objects.create(
            preferred_username="dough",
            type="Person",
            user=self.user,
        )

    def test_actor_view_returns_as2_with_public_key(self):
        response = self.client.get("/actors/dough/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(body["type"], "Person")
        self.assertEqual(body["preferredUsername"], "dough")
        self.assertEqual(body["id"], f"{TEST_ORIGIN}/actors/dough")
        self.assertEqual(body["inbox"], f"{TEST_ORIGIN}/actors/dough/inbox")
        self.assertEqual(body["outbox"], f"{TEST_ORIGIN}/actors/dough/outbox")

        pk_block = body["publicKey"]
        self.assertEqual(pk_block["id"], f"{TEST_ORIGIN}/actors/dough#main-key")
        self.assertEqual(pk_block["owner"], f"{TEST_ORIGIN}/actors/dough")
        self.assertTrue(
            pk_block["publicKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")
        )

    def test_actor_view_persists_keypair_on_first_hit(self):
        # Pre-condition: keypair NULL.
        self.assertFalse(self.actor.has_keypair)

        self.client.get("/actors/dough/")
        self.actor.refresh_from_db()
        self.assertTrue(self.actor.has_keypair)
        self.assertTrue(
            self.actor.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")
        )

    def test_actor_view_includes_user_display_name(self):
        response = self.client.get("/actors/dough/")
        body = response.json()
        self.assertEqual(body["name"], "Danny Noonan")

    def test_actor_view_unknown_returns_404(self):
        response = self.client.get("/actors/nobody/")
        self.assertEqual(response.status_code, 404)


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestLazyKeypairConcurrency(TransactionTestCase):
    """SELECT FOR UPDATE inside transaction.atomic() must serialise
    concurrent first-hits so the row gets one and only one keypair.

    TransactionTestCase rather than TestCase because the row-level lock
    in ``_ensure_keypair`` only takes hold across real transactions —
    TestCase wraps every test in an outer transaction that prevents the
    inner SELECT FOR UPDATE from seeing the contention we're testing.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="racer", password="pass")
        self.actor = Actor.objects.create(
            preferred_username="racer",
            type="Person",
            user=self.user,
        )

    def test_concurrent_first_hits_yield_single_keypair(self):
        def hit():
            try:
                return self.client.get("/actors/racer/").status_code
            finally:
                # Each worker thread holds its own connection; closing
                # avoids "too many open" warnings when the pool is small.
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: hit(), range(4)))

        self.assertTrue(all(s == 200 for s in results))

        self.actor.refresh_from_db()
        self.assertTrue(self.actor.has_keypair)

        # No duplicate Actor rows minted under the race.
        self.assertEqual(
            Actor.objects.filter(preferred_username="racer").count(), 1
        )


class TestMgmtCommands(TestCase):
    """bootstrap_instance_actor + generate_federation_actors are
    idempotent — second invocation must not raise, must not duplicate."""

    def test_bootstrap_instance_actor_idempotent(self):
        out1 = StringIO()
        call_command("bootstrap_instance_actor", stdout=out1)
        self.assertIn("Created Instance Actor", out1.getvalue())

        out2 = StringIO()
        call_command("bootstrap_instance_actor", stdout=out2)
        self.assertIn("already exists", out2.getvalue())

        self.assertEqual(
            Actor.objects.filter(preferred_username="instance").count(), 1
        )
        actor = Actor.objects.get(preferred_username="instance")
        self.assertEqual(actor.type, "Application")
        self.assertIsNone(actor.user_id)

    def test_generate_federation_actors_idempotent(self):
        User.objects.create_user(username="alice", password="pass")
        User.objects.create_user(username="bob", password="pass")

        out1 = StringIO()
        call_command("generate_federation_actors", stdout=out1)
        self.assertIn("created=2", out1.getvalue())

        # Re-run: nothing new.
        out2 = StringIO()
        call_command("generate_federation_actors", stdout=out2)
        self.assertIn("created=0", out2.getvalue())
        self.assertIn("existing=2", out2.getvalue())

        self.assertEqual(
            Actor.objects.filter(type="Person").count(), 2
        )

    def test_generate_federation_actors_skips_instance_username(self):
        # A pre-existing user literally called "instance" must not
        # collide with the reserved Instance Actor row.
        User.objects.create_user(username="instance", password="pass")

        out = StringIO()
        call_command("generate_federation_actors", stdout=out)
        self.assertIn("skipped=1", out.getvalue())
        # No Person actor minted for the colliding user.
        self.assertFalse(
            Actor.objects.filter(
                preferred_username="instance", type="Person"
            ).exists()
        )
