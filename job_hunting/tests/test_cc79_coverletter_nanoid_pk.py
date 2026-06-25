"""CC-77 #79 — CoverLetter integer PK -> 10-char NanoID PK (true PK swap).

Beyond the NanoIDModel contract we assert the one FK that references
``cover_letter(id)`` — ``job_application.cover_letter_id`` (SET_NULL,
nullable) — round-trips with the NanoID value and traverses both ways, and
that a NULL cover_letter is still accepted.
"""

import re

from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    CoverLetter,
    JobApplication,
    NanoIDModel,
)


class CoverLetterNanoIdPkContractTests(TestCase):
    def test_coverletter_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(CoverLetter, NanoIDModel))
        pk_field = CoverLetter._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_coverletter_gets_nanoid_pk(self):
        cl = CoverLetter.objects.create(content="Dear hiring manager")
        self.assertIsInstance(cl.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, cl.pk), cl.pk)
        self.assertEqual(
            CoverLetter.objects.get(pk=cl.pk).content, "Dear hiring manager"
        )

    def test_distinct_pks(self):
        a = CoverLetter.objects.create()
        b = CoverLetter.objects.create()
        self.assertNotEqual(a.pk, b.pk)


class CoverLetterDependentForeignKeyTests(TestCase):
    def test_job_application_cover_letter_fk_round_trips(self):
        cl = CoverLetter.objects.create()
        app = JobApplication.objects.create(cover_letter=cl)
        app.refresh_from_db()
        self.assertEqual(app.cover_letter_id, cl.pk)
        self.assertIsInstance(app.cover_letter_id, str)
        self.assertEqual(list(cl.application.all()), [app])

    def test_cover_letter_is_nullable_on_application(self):
        app = JobApplication.objects.create(cover_letter=None)
        app.refresh_from_db()
        self.assertIsNone(app.cover_letter_id)

    def test_set_null_on_cover_letter_delete(self):
        cl = CoverLetter.objects.create()
        app = JobApplication.objects.create(cover_letter=cl)
        cl.delete()
        app.refresh_from_db()
        self.assertIsNone(app.cover_letter_id)
