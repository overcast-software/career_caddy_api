"""CC-77 #79 — ScrapeProfile integer PK -> 10-char NanoID PK (true PK swap).

ScrapeProfile is a leaf (nothing FKs to it; Scrape associates by hostname
string, not an FK). Beyond the NanoIDModel contract we assert the
single-column ``hostname`` UNIQUE survives the swap.
"""

import re

from django.db import IntegrityError, transaction
from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    NanoIDModel,
    ScrapeProfile,
)


class ScrapeProfileNanoIdPkContractTests(TestCase):
    def test_scrapeprofile_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(ScrapeProfile, NanoIDModel))
        pk_field = ScrapeProfile._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_scrapeprofile_gets_nanoid_pk(self):
        sp = ScrapeProfile.objects.create(hostname="example.com")
        self.assertIsInstance(sp.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, sp.pk), sp.pk)
        self.assertEqual(ScrapeProfile.objects.get(pk=sp.pk).hostname, "example.com")

    def test_distinct_pks(self):
        a = ScrapeProfile.objects.create(hostname="a.com")
        b = ScrapeProfile.objects.create(hostname="b.com")
        self.assertNotEqual(a.pk, b.pk)

    def test_hostname_unique_survives_swap(self):
        ScrapeProfile.objects.create(hostname="dup.com")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ScrapeProfile.objects.create(hostname="dup.com")
