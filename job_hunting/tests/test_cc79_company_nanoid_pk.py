"""CC-77 #79 — Company integer PK -> 10-char NanoID PK (true PK swap).

These tests run against the post-migration schema: the test DB is built by
applying every migration, including ``0124_company_nanoid_pk_swap``. A broken
forward swap fails the whole suite at DB-build time, so importing + querying
Company is itself a smoke test of the migration.

Beyond the NanoIDModel contract we assert the self-FK (``canonical``) round-trips
and its ``company_canonical_not_self`` CheckConstraint still rejects self-loops,
that every external FK round-trips and honours CASCADE / SET_NULL on delete, and
that the ``federation_follower_unique_company_remote`` partial UNIQUE survives on
the NanoID columns.
"""

import re

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    Actor,
    Company,
    CompanyAlias,
    CoverLetter,
    Experience,
    FederationFollower,
    JobApplication,
    JobPost,
    NanoIDModel,
    Question,
    Scrape,
)

User = get_user_model()


class CompanyNanoIdPkContractTests(TestCase):
    def test_company_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Company, NanoIDModel))
        pk_field = Company._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_company_gets_nanoid_pk(self):
        c = Company.objects.create(name="Acme")
        self.assertIsInstance(c.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, c.pk), c.pk)
        self.assertEqual(Company.objects.get(pk=c.pk).name, "Acme")

    def test_distinct_pks(self):
        a = Company.objects.create(name="A")
        b = Company.objects.create(name="B")
        self.assertNotEqual(a.pk, b.pk)


class CompanySelfForeignKeyTests(TestCase):
    def test_canonical_self_fk_round_trips(self):
        canonical = Company.objects.create(name="Canonical Co")
        alias = Company.objects.create(name="Alias Co", canonical=canonical)
        alias.refresh_from_db()
        self.assertEqual(alias.canonical_id, canonical.pk)
        self.assertIsInstance(alias.canonical_id, str)
        self.assertEqual(list(canonical.aliases.all()), [alias])

    def test_canonical_set_null_on_canonical_delete(self):
        canonical = Company.objects.create(name="Root")
        alias = Company.objects.create(name="Variant", canonical=canonical)
        canonical.delete()
        alias.refresh_from_db()
        self.assertIsNone(alias.canonical_id)

    def test_company_canonical_not_self_constraint(self):
        c = Company.objects.create(name="Loopy")
        c.canonical_id = c.id
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                c.save(update_fields=["canonical"])

    def test_mark_as_alias_of_service_verb(self):
        canonical = Company.objects.create(name="True Co")
        alias = Company.objects.create(name="Other Co")
        alias.mark_as_alias_of(canonical.id)
        alias.refresh_from_db()
        self.assertEqual(alias.canonical_id, canonical.pk)


class CompanyCascadeForeignKeyTests(TestCase):
    """CASCADE dependents: federation_actors / federation_followers / company_alias."""

    def setUp(self):
        self.company = Company.objects.create(name="Cascade Co")

    def test_federation_actor_fk_round_trips_and_cascades(self):
        actor = Actor.objects.create(
            company=self.company, type="Organization", preferred_username="cascadeco"
        )
        actor.refresh_from_db()
        self.assertEqual(actor.company_id, self.company.pk)
        self.assertIsInstance(actor.company_id, str)
        self.company.delete()
        self.assertEqual(Actor.objects.filter(pk=actor.pk).count(), 0)

    def test_company_alias_fk_round_trips_and_cascades(self):
        alias = CompanyAlias.objects.create(
            company=self.company, name="Cascade Inc", name_slug="cascade-inc",
            source="manual",
        )
        alias.refresh_from_db()
        self.assertEqual(alias.company_id, self.company.pk)
        self.company.delete()
        self.assertEqual(CompanyAlias.objects.filter(pk=alias.pk).count(), 0)

    def test_federation_follower_fk_round_trips_and_cascades(self):
        f = FederationFollower.objects.create(
            company=self.company,
            actor_uri="https://mastodon.social/users/a",
            inbox_uri="https://mastodon.social/users/a/inbox",
            instance_host="mastodon.social",
        )
        f.refresh_from_db()
        self.assertEqual(f.company_id, self.company.pk)
        self.company.delete()
        self.assertEqual(FederationFollower.objects.filter(pk=f.pk).count(), 0)


class CompanySetNullForeignKeyTests(TestCase):
    """SET_NULL dependents: job_post / scrape / cover_letter / job_application /
    experience / question."""

    def setUp(self):
        self.company = Company.objects.create(name="SetNull Co")

    def test_job_post_company_fk_round_trips_and_set_null(self):
        jp = JobPost.objects.create(title="Eng", company=self.company)
        jp.refresh_from_db()
        self.assertEqual(jp.company_id, self.company.pk)
        self.assertIsInstance(jp.company_id, str)
        self.assertEqual(list(self.company.job_posts.all()), [jp])
        self.company.delete()
        jp.refresh_from_db()
        self.assertIsNone(jp.company_id)

    def test_scrape_company_set_null(self):
        s = Scrape.objects.create(company=self.company)
        self.assertEqual(s.company_id, self.company.pk)
        self.company.delete()
        s.refresh_from_db()
        self.assertIsNone(s.company_id)

    def test_cover_letter_company_set_null(self):
        cl = CoverLetter.objects.create(company=self.company)
        self.assertEqual(cl.company_id, self.company.pk)
        self.company.delete()
        cl.refresh_from_db()
        self.assertIsNone(cl.company_id)

    def test_job_application_company_set_null(self):
        app = JobApplication.objects.create(company=self.company)
        self.assertEqual(app.company_id, self.company.pk)
        self.company.delete()
        app.refresh_from_db()
        self.assertIsNone(app.company_id)

    def test_experience_company_set_null(self):
        exp = Experience.objects.create(title="SWE", company=self.company)
        self.assertEqual(exp.company_id, self.company.pk)
        self.company.delete()
        exp.refresh_from_db()
        self.assertIsNone(exp.company_id)

    def test_question_company_set_null(self):
        q = Question.objects.create(company=self.company)
        self.assertEqual(q.company_id, self.company.pk)
        self.company.delete()
        q.refresh_from_db()
        self.assertIsNone(q.company_id)


class CompanyFederationFollowerPartialUniqueTests(TestCase):
    """The per-company partial UNIQUE(company, actor_uri) survives the swap."""

    def test_unique_company_remote(self):
        company = Company.objects.create(name="Uniq Co")
        FederationFollower.objects.create(
            company=company,
            actor_uri="https://m.example/users/x",
            inbox_uri="https://m.example/users/x/inbox",
            instance_host="m.example",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FederationFollower.objects.create(
                    company=company,
                    actor_uri="https://m.example/users/x",
                    inbox_uri="https://m.example/users/x/inbox",
                    instance_host="m.example",
                )
