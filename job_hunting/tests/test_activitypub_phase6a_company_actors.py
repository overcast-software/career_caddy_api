"""Phase 6a ActivityPub Company / Organization actor tests.

Pins the contracts laid out in
``notes.org/Plans/PLAN Fediverse Phase 6/Phase 6a — Company Organization actors``:

- Actor model XOR constraint: (user, company) is one-or-neither, not both.
- ``backfill_company_slugs`` management command: idempotent, slugifies
  ``name`` with id-collision suffix, fills empty-slug rows only.
- ``GET /companies/<slug>/`` content negotiation:
  AS2 (Accept: application/activity+json) → Organization JSON-LD,
  default → JSON:API Company linkage.
- ``GET /companies/<slug>/outbox`` AS2 OrderedCollection of Create(Note)
  envelopes attributed to the Company actor URI.
- WebFinger resolves ``acct:<slug>@<host>`` to the Company actor URI.
- Signal-side dispatch: JP save with ``company.federation_enabled=True``
  invokes ``enqueue_jobpost_activity_for_company`` with "create".

Scope (strict): 6a only — no inbound JP ingest (6b), no employer
self-claim (6d), no Update/Delete dispatch attributed to a Company
actor (6e). Those land in their own dispatches.
"""
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from job_hunting.models import Actor, Company, JobPost
from job_hunting.models.actor import (
    ACTOR_TYPE_ORGANIZATION,
    ACTOR_TYPE_PERSON,
)
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


TEST_ORIGIN = "http://testserver"


# ---------------------------------------------------------------------------
# Schema: Actor.user/company mutual exclusivity
# ---------------------------------------------------------------------------


class TestActorUserCompanyExclusivity(TestCase):
    """The Actor XOR constraint must accept (user-only / company-only /
    neither) and reject (both-set)."""

    def test_person_actor_user_only_ok(self):
        user = User.objects.create_user(username="dough", password="pass")
        actor = Actor.objects.create(
            preferred_username="dough", type=ACTOR_TYPE_PERSON, user=user,
        )
        self.assertEqual(actor.user_id, user.id)
        self.assertIsNone(actor.company_id)

    def test_organization_actor_company_only_ok(self):
        company = Company.objects.create(name="Acme", slug="acme")
        actor = Actor.objects.create(
            preferred_username="acme",
            type=ACTOR_TYPE_ORGANIZATION,
            company=company,
        )
        self.assertEqual(actor.company_id, company.id)
        self.assertIsNone(actor.user_id)

    def test_instance_actor_both_null_ok(self):
        actor = Actor.objects.create(
            preferred_username="instance", type="Application",
        )
        self.assertIsNone(actor.user_id)
        self.assertIsNone(actor.company_id)

    def test_both_set_rejected_by_db_check(self):
        user = User.objects.create_user(username="dough", password="pass")
        company = Company.objects.create(name="Acme", slug="acme")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Actor.objects.create(
                    preferred_username="both",
                    type=ACTOR_TYPE_ORGANIZATION,
                    user=user,
                    company=company,
                )


# ---------------------------------------------------------------------------
# backfill_company_slugs management command
# ---------------------------------------------------------------------------


class TestBackfillCompanySlugs(TestCase):
    """``manage.py backfill_company_slugs`` — idempotent, collision-suffixed."""

    def test_populates_empty_slugs(self):
        a = Company.objects.create(name="Acme Corp")
        b = Company.objects.create(name="Beta Industries")
        self.assertIsNone(a.slug)
        self.assertIsNone(b.slug)

        out = StringIO()
        call_command("backfill_company_slugs", stdout=out)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.slug, "acme-corp")
        self.assertEqual(b.slug, "beta-industries")

    def test_idempotent_on_already_set(self):
        # Pre-existing slug stays put across re-runs.
        c = Company.objects.create(name="Gamma", slug="gamma-manual")
        call_command("backfill_company_slugs", stdout=StringIO())
        c.refresh_from_db()
        self.assertEqual(c.slug, "gamma-manual")
        # Second run — still no change.
        call_command("backfill_company_slugs", stdout=StringIO())
        c.refresh_from_db()
        self.assertEqual(c.slug, "gamma-manual")

    def test_collision_appends_id_suffix(self):
        # Distinct Company.name values that slugify identically — Django's
        # slugify lowercases, so "Acme" and "ACME" collapse to the same
        # base slug and we exercise the collision-suffix branch.
        c1 = Company.objects.create(name="Acme")
        c2 = Company.objects.create(name="ACME")

        call_command("backfill_company_slugs", stdout=StringIO())
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c1.slug, "acme")
        # Collision suffix is the row's own id for determinism.
        self.assertEqual(c2.slug, f"acme-{c2.id}")

    def test_all_punctuation_name_falls_back_to_company_id(self):
        c = Company.objects.create(name="!!! ???")
        call_command("backfill_company_slugs", stdout=StringIO())
        c.refresh_from_db()
        self.assertEqual(c.slug, f"company-{c.id}")


# ---------------------------------------------------------------------------
# /companies/<slug>/ content negotiation
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestCompanyActorContentNegotiation(TestCase):
    """``GET /companies/<slug>/`` — AS2 for federation, JSON:API for browsers."""

    def setUp(self):
        self.company = Company.objects.create(
            name="Acme Corp", display_name="Acme", slug="acme",
        )

    def test_404_when_slug_not_found(self):
        response = self.client.get("/companies/unknown/")
        self.assertEqual(response.status_code, 404)

    def test_as2_accept_returns_organization_jsonld(self):
        response = self.client.get(
            "/companies/acme/",
            HTTP_ACCEPT="application/activity+json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        actor_uri = f"{TEST_ORIGIN}/companies/acme"
        self.assertEqual(body["id"], actor_uri)
        self.assertEqual(body["type"], "Organization")
        self.assertEqual(body["preferredUsername"], "acme")
        self.assertEqual(body["url"], actor_uri)
        self.assertEqual(body["inbox"], f"{actor_uri}/inbox")
        self.assertEqual(body["outbox"], f"{actor_uri}/outbox")
        self.assertEqual(body["followers"], f"{actor_uri}/followers")
        self.assertEqual(body["name"], "Acme")  # display_name preferred
        # Public key materialized lazily on first AS2 hit.
        self.assertIn("publicKey", body)
        self.assertTrue(body["publicKey"]["publicKeyPem"].startswith("-----"))

    def test_ld_json_accept_also_returns_as2(self):
        # Mastodon sometimes sends application/ld+json with the AS2
        # profile parameter; the prefix match must still route AS2.
        response = self.client.get(
            "/companies/acme/",
            HTTP_ACCEPT='application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")

    def test_browser_default_returns_jsonapi_linkage(self):
        response = self.client.get("/companies/acme/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.api+json")
        body = response.json()
        self.assertEqual(body["data"]["type"], "company")
        self.assertEqual(body["data"]["id"], str(self.company.id))
        self.assertEqual(body["data"]["attributes"]["slug"], "acme")
        self.assertEqual(
            body["data"]["attributes"]["federation-enabled"], False
        )

    def test_lazy_actor_materialization(self):
        # First AS2 hit creates the Organization Actor + keypair.
        self.assertEqual(Actor.objects.filter(company=self.company).count(), 0)
        self.client.get("/companies/acme/", HTTP_ACCEPT="application/activity+json")
        actor = Actor.objects.get(company=self.company)
        self.assertEqual(actor.type, ACTOR_TYPE_ORGANIZATION)
        self.assertEqual(actor.preferred_username, "acme")
        self.assertTrue(actor.has_keypair)
        # Second hit reuses the row + keypair.
        self.client.get("/companies/acme/", HTTP_ACCEPT="application/activity+json")
        self.assertEqual(Actor.objects.filter(company=self.company).count(), 1)

    def test_icon_emitted_when_avatar_url_set(self):
        self.client.get("/companies/acme/", HTTP_ACCEPT="application/activity+json")
        actor = Actor.objects.get(company=self.company)
        actor.avatar_url = "https://example.com/logo.png"
        actor.save()
        body = self.client.get(
            "/companies/acme/", HTTP_ACCEPT="application/activity+json",
        ).json()
        self.assertEqual(
            body["icon"], {"type": "Image", "url": "https://example.com/logo.png"}
        )


# ---------------------------------------------------------------------------
# /companies/<slug>/outbox
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN, ACTIVITYPUB_OUTBOX_PAGE_SIZE=20)
class TestCompanyOutbox(TestCase):
    """``GET /companies/<slug>/outbox`` — paginated Create(Note) collection."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        Actor.objects.create(
            preferred_username="dough", type=ACTOR_TYPE_PERSON, user=self.user,
            private_key_pem="-----PRETEND-----",
            public_key_pem="-----PRETEND-----",
        )
        self.company = Company.objects.create(
            name="Acme", slug="acme", federation_enabled=True,
        )
        self.other_company = Company.objects.create(
            name="Beta", slug="beta",
        )
        # Two public JP for Acme, one for Beta, one private for Acme.
        self.jp1 = JobPost.objects.create(
            created_by=self.user, title="Engineer",
            description="role 1", link="https://example.com/jobs/1",
            company=self.company,
        )
        self.jp2 = JobPost.objects.create(
            created_by=self.user, title="Manager",
            description="role 2", link="https://example.com/jobs/2",
            company=self.company,
        )
        self.jp_private = JobPost.objects.create(
            created_by=self.user, title="Private",
            description="hidden", link="https://example.com/jobs/3",
            company=self.company, audience=[],
        )
        self.jp_other = JobPost.objects.create(
            created_by=self.user, title="Other Co Role",
            description="x", link="https://example.com/jobs/4",
            company=self.other_company,
        )

    def test_outbox_metadata_shape(self):
        response = self.client.get("/companies/acme/outbox")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/activity+json")
        body = response.json()
        self.assertEqual(body["type"], "OrderedCollection")
        self.assertEqual(body["totalItems"], 2)
        self.assertEqual(
            body["id"], f"{TEST_ORIGIN}/companies/acme/outbox"
        )
        self.assertIn("first", body)
        self.assertIn("last", body)

    def test_outbox_page_returns_attributed_create_activities(self):
        response = self.client.get("/companies/acme/outbox?page=1")
        body = response.json()
        actor_uri = f"{TEST_ORIGIN}/companies/acme"
        self.assertEqual(body["type"], "OrderedCollectionPage")
        self.assertEqual(len(body["orderedItems"]), 2)
        for activity in body["orderedItems"]:
            self.assertEqual(activity["type"], "Create")
            self.assertEqual(activity["actor"], actor_uri)
            self.assertEqual(activity["to"], [AS2_PUBLIC])
            self.assertEqual(activity["cc"], [f"{actor_uri}/followers"])
            self.assertEqual(activity["object"]["attributedTo"], actor_uri)
            self.assertEqual(activity["object"]["type"], "Note")

    def test_private_post_excluded(self):
        body = self.client.get("/companies/acme/outbox?page=1").json()
        ids = [item["object"]["id"] for item in body["orderedItems"]]
        self.assertNotIn(
            f"{TEST_ORIGIN}/job-posts/{self.jp_private.id}", ids
        )

    def test_other_company_post_excluded(self):
        body = self.client.get("/companies/acme/outbox?page=1").json()
        ids = [item["object"]["id"] for item in body["orderedItems"]]
        self.assertNotIn(
            f"{TEST_ORIGIN}/job-posts/{self.jp_other.id}", ids
        )

    def test_404_when_company_slug_unknown(self):
        response = self.client.get("/companies/nope/outbox")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# WebFinger — Company slug resolution
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestWebFingerCompanyResolution(TestCase):
    """WebFinger resolves ``acct:<slug>@<host>`` against Company.slug."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme", slug="acme")

    def test_resolves_company_slug_to_company_actor_uri(self):
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "acct:acme@testserver"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["subject"], "acct:acme@testserver")
        self_link = next(link for link in body["links"] if link["rel"] == "self")
        self.assertEqual(
            self_link["href"], f"{TEST_ORIGIN}/companies/acme"
        )

    def test_person_slug_lookup_still_works(self):
        user = User.objects.create_user(username="dough", password="pass")
        Actor.objects.create(
            preferred_username="dough", type=ACTOR_TYPE_PERSON, user=user,
        )
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "acct:dough@testserver"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self_link = next(link for link in body["links"] if link["rel"] == "self")
        self.assertEqual(self_link["href"], f"{TEST_ORIGIN}/actors/dough")

    def test_unknown_handle_404(self):
        response = self.client.get(
            "/.well-known/webfinger",
            {"resource": "acct:nobody@testserver"},
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Dispatch trigger on JobPost save with company.federation_enabled
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestCompanyDispatchTrigger(TestCase):
    """JP save fires ``enqueue_jobpost_activity_for_company`` when the
    Company has opted into federation."""

    def setUp(self):
        self.user = User.objects.create_user(username="dough", password="pass")
        Actor.objects.create(
            preferred_username="dough", type=ACTOR_TYPE_PERSON, user=self.user,
            private_key_pem="-----PRETEND-----",
            public_key_pem="-----PRETEND-----",
        )
        self.fed_company = Company.objects.create(
            name="Acme", slug="acme", federation_enabled=True,
        )
        self.muted_company = Company.objects.create(
            name="Beta", slug="beta", federation_enabled=False,
        )

    def test_save_with_federation_enabled_company_invokes_dispatch(self):
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity_for_company"
        ) as enq_co:
            JobPost.objects.create(
                created_by=self.user, title="Hire",
                description="here", link="https://example.com/jobs/1",
                company=self.fed_company,
            )
        kinds = [call.args[1] for call in enq_co.call_args_list]
        self.assertIn("create", kinds)

    def test_save_with_muted_company_does_not_invoke_dispatch(self):
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity_for_company"
        ) as enq_co:
            JobPost.objects.create(
                created_by=self.user, title="Hire",
                description="here", link="https://example.com/jobs/2",
                company=self.muted_company,
            )
        self.assertEqual(enq_co.call_args_list, [])

    def test_save_with_no_company_does_not_invoke_dispatch(self):
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity_for_company"
        ) as enq_co:
            JobPost.objects.create(
                created_by=self.user, title="Hire",
                description="here", link="https://example.com/jobs/3",
            )
        self.assertEqual(enq_co.call_args_list, [])

    def test_private_save_does_not_invoke_dispatch(self):
        with patch(
            "job_hunting.signals.federation.enqueue_jobpost_activity_for_company"
        ) as enq_co:
            JobPost.objects.create(
                created_by=self.user, title="Hire",
                description="here", link="https://example.com/jobs/4",
                company=self.fed_company, audience=[],
            )
        self.assertEqual(enq_co.call_args_list, [])
