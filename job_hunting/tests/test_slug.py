"""Tests for the Company-alias slug helpers (Phase A dedupe redesign).

Covers the four normalization axes that have actually bitten the
project:

- unicode dashes / hyphen-minus / smart quotes (JP 1329 vs JP 3323
  was an en-dash vs hyphen-minus drift);
- case + whitespace collapse;
- legal-entity suffix stripping ("Corporation", "Insurance Company",
  "LLC", …);
- combinations of all of the above.
"""

from django.test import TestCase

from job_hunting.lib.slug import slug, strip_corp_suffix


class TestSlug(TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(slug(""), "")
        self.assertEqual(slug("   "), "")
        self.assertEqual(slug(None), "")

    def test_lowercase(self):
        self.assertEqual(slug("Acme"), "acme")
        self.assertEqual(slug("ACME"), "acme")

    def test_collapses_whitespace(self):
        self.assertEqual(slug("Acme  Corp"), "acme-corp")
        self.assertEqual(slug("Acme\tCorp"), "acme-corp")
        self.assertEqual(slug("  Acme   Corp  "), "acme-corp")

    def test_unicode_dashes_normalize_to_hyphen(self):
        # U+2010 hyphen, U+2011 NB-hyphen, U+2012 figure dash,
        # U+2013 en-dash, U+2014 em-dash, U+2015 horizontal bar,
        # U+2212 math minus.
        cases = [
            "Acme‐Corp",
            "Acme‑Corp",
            "Acme‒Corp",
            "Acme–Corp",
            "Acme—Corp",
            "Acme―Corp",
            "Acme−Corp",
            "Acme-Corp",
        ]
        slugs = {slug(c) for c in cases}
        self.assertEqual(slugs, {"acme-corp"})

    def test_smart_quotes_collapse(self):
        # The smart-quote variants are stripped because they're
        # neither alphanumeric nor hyphen-minus — but the surrounding
        # letters survive.
        self.assertEqual(slug("Driver’s License"), "drivers-license")
        self.assertEqual(slug("Driver‘s License"), "drivers-license")
        self.assertEqual(slug("Driver's License"), "drivers-license")

    def test_strips_punctuation(self):
        self.assertEqual(slug("Acme, Inc."), "acme-inc")
        self.assertEqual(slug("Acme & Sons"), "acme-sons")
        self.assertEqual(slug("Acme/Corp"), "acmecorp")

    def test_idempotent(self):
        self.assertEqual(slug(slug("Acme — Corp.")), slug("Acme — Corp."))


class TestStripCorpSuffix(TestCase):
    def test_empty_input(self):
        self.assertEqual(strip_corp_suffix(""), "")
        self.assertEqual(strip_corp_suffix(None), "")

    def test_no_suffix(self):
        self.assertEqual(strip_corp_suffix("Acme"), "Acme")

    def test_single_suffix(self):
        self.assertEqual(strip_corp_suffix("Acme Corporation"), "Acme")
        self.assertEqual(strip_corp_suffix("Acme Corp"), "Acme")
        self.assertEqual(strip_corp_suffix("Acme Inc"), "Acme")
        self.assertEqual(strip_corp_suffix("Acme LLC"), "Acme")
        self.assertEqual(strip_corp_suffix("Acme Limited"), "Acme")
        self.assertEqual(strip_corp_suffix("Acme Holdings"), "Acme")

    def test_suffix_with_punctuation(self):
        self.assertEqual(strip_corp_suffix("Acme, Inc."), "Acme")
        self.assertEqual(strip_corp_suffix("Acme Inc."), "Acme")
        self.assertEqual(strip_corp_suffix("Acme, LLC"), "Acme")

    def test_multi_word_suffix(self):
        self.assertEqual(
            strip_corp_suffix("Allstate Insurance Company"), "Allstate"
        )
        self.assertEqual(
            strip_corp_suffix("Acme Holdings Group"), "Acme"
        )

    def test_chained_suffixes(self):
        self.assertEqual(strip_corp_suffix("Acme Holdings, Inc."), "Acme")
        self.assertEqual(strip_corp_suffix("Acme Holdings, LLC."), "Acme")

    def test_case_insensitive(self):
        self.assertEqual(strip_corp_suffix("Acme CORPORATION"), "Acme")
        self.assertEqual(strip_corp_suffix("Acme corporation"), "Acme")

    def test_idempotent(self):
        once = strip_corp_suffix("Acme Holdings, Inc.")
        twice = strip_corp_suffix(once)
        self.assertEqual(once, twice)


class TestSlugStripCorpSuffixCombo(TestCase):
    """The composed pipeline ``slug(strip_corp_suffix(x))`` is what
    Company.find_by_alias actually uses. Verify the JP 1162/1164
    Allstate regression — "Allstate Corporation" and
    "Allstate Insurance Company" must produce the same slug."""

    def test_allstate_variants_collapse(self):
        a = slug(strip_corp_suffix("Allstate Corporation"))
        b = slug(strip_corp_suffix("Allstate Insurance Company"))
        c = slug(strip_corp_suffix("Allstate"))
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertEqual(a, "allstate")

    def test_endash_title_collapse(self):
        # State Farm – Hartford vs State Farm - Hartford
        a = slug(strip_corp_suffix("State Farm – Hartford"))
        b = slug(strip_corp_suffix("State Farm - Hartford"))
        self.assertEqual(a, b)

    def test_acme_holdings_variants(self):
        forms = [
            "Acme",
            "Acme Inc",
            "Acme Inc.",
            "Acme, Inc.",
            "Acme Holdings",
            "Acme Holdings, LLC",
            "Acme Holdings Group",
        ]
        slugs = {slug(strip_corp_suffix(f)) for f in forms}
        self.assertEqual(slugs, {"acme"})
