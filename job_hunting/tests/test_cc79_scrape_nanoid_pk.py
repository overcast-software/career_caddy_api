"""CC-77 #79 — Scrape integer PK -> 10-char NanoID PK (true PK swap).

Scrape is the second swapped table with a self-FK (after JobPost). Beyond
the NanoIDModel contract we assert the self-FK and all three dependent FKs
that reference ``scrape(id)`` round-trip with the NanoID value and traverse
both ways, and that CASCADE / SET_NULL deletes still behave.
"""

import re

from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    JobPost,
    JobPostDescriptionDecision,
    JobPostOverwriteDecision,
    NanoIDModel,
    Scrape,
    ScrapeStatus,
)


class ScrapeNanoIdPkContractTests(TestCase):
    def test_scrape_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Scrape, NanoIDModel))
        pk_field = Scrape._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_scrape_gets_nanoid_pk(self):
        sc = Scrape.objects.create(url="https://example.com/job")
        self.assertIsInstance(sc.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, sc.pk), sc.pk)
        self.assertEqual(
            Scrape.objects.get(pk=sc.pk).url, "https://example.com/job"
        )

    def test_distinct_pks(self):
        a = Scrape.objects.create()
        b = Scrape.objects.create()
        self.assertNotEqual(a.pk, b.pk)


class ScrapeSelfForeignKeyTests(TestCase):
    def test_source_scrape_round_trips(self):
        parent = Scrape.objects.create(url="https://x/parent")
        child = Scrape.objects.create(url="https://x/child", source_scrape=parent)
        child.refresh_from_db()
        self.assertEqual(child.source_scrape_id, parent.pk)
        self.assertIsInstance(child.source_scrape_id, str)
        self.assertEqual(list(parent.child_scrapes.all()), [child])

    def test_source_scrape_set_null_on_parent_delete(self):
        parent = Scrape.objects.create()
        child = Scrape.objects.create(source_scrape=parent)
        parent.delete()
        child.refresh_from_db()
        self.assertIsNone(child.source_scrape_id)


class ScrapeDependentForeignKeyTests(TestCase):
    def setUp(self):
        self.sc = Scrape.objects.create(url="https://x/y")
        self.jp = JobPost.objects.create(title="Backend Engineer")

    def test_scrape_status_fk_round_trips(self):
        st = ScrapeStatus.objects.create(scrape=self.sc)
        st.refresh_from_db()
        self.assertEqual(st.scrape_id, self.sc.pk)
        self.assertIsInstance(st.scrape_id, str)
        self.assertEqual(list(self.sc.scrape_statuses.all()), [st])

    def test_scrape_status_cascade_delete(self):
        ScrapeStatus.objects.create(scrape=self.sc)
        self.assertEqual(ScrapeStatus.objects.count(), 1)
        self.sc.delete()
        self.assertEqual(ScrapeStatus.objects.count(), 0)

    def test_overwrite_decision_triggering_scrape_round_trips(self):
        d = JobPostOverwriteDecision.objects.create(
            job_post=self.jp, triggering_scrape=self.sc
        )
        d.refresh_from_db()
        self.assertEqual(d.triggering_scrape_id, self.sc.pk)
        self.assertEqual(list(self.sc.overwrite_decisions.all()), [d])

    def test_description_decision_triggering_scrape_round_trips(self):
        d = JobPostDescriptionDecision.objects.create(
            job_post=self.jp,
            triggering_scrape=self.sc,
            existing_description_hash="a" * 8,
            new_description_hash="b" * 8,
            existing_word_count=10,
            new_word_count=20,
            choice="use_new",
            confidence="high",
        )
        d.refresh_from_db()
        self.assertEqual(d.triggering_scrape_id, self.sc.pk)
        self.assertEqual(list(self.sc.description_decisions.all()), [d])

    def test_triggering_scrape_set_null_on_scrape_delete(self):
        d = JobPostOverwriteDecision.objects.create(
            job_post=self.jp, triggering_scrape=self.sc
        )
        self.sc.delete()
        d.refresh_from_db()
        self.assertIsNone(d.triggering_scrape_id)
