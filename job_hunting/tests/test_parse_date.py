import unittest
from datetime import date

from job_hunting.lib.services.ingest_resume import (
    IngestResume,
    _canonicalize_date_string,
    _split_date_range,
)


class TestCanonicalizeDateString(unittest.TestCase):
    def test_iso_year_only(self):
        self.assertEqual(_canonicalize_date_string("2020"), "2020")

    def test_iso_year_month(self):
        self.assertEqual(_canonicalize_date_string("2020-01"), "2020-01")
        self.assertEqual(_canonicalize_date_string("2020-1"), "2020-01")

    def test_iso_year_month_day_truncates_to_month(self):
        self.assertEqual(_canonicalize_date_string("2020-01-15"), "2020-01")

    def test_month_abbrev(self):
        self.assertEqual(_canonicalize_date_string("Jan 2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("jan 2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("JAN 2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("Jan. 2020"), "2020-01")

    def test_month_full_name(self):
        self.assertEqual(_canonicalize_date_string("January 2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("December 1999"), "1999-12")

    def test_sept_nonstandard(self):
        self.assertEqual(_canonicalize_date_string("Sept 2021"), "2021-09")

    def test_month_name_with_day(self):
        self.assertEqual(_canonicalize_date_string("Jan 15, 2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("January 15 2020"), "2020-01")

    def test_numeric_slash(self):
        self.assertEqual(_canonicalize_date_string("01/2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("1/2020"), "2020-01")
        self.assertEqual(_canonicalize_date_string("12-2020"), "2020-12")

    def test_present_variants(self):
        self.assertEqual(_canonicalize_date_string("present"), "present")
        self.assertEqual(_canonicalize_date_string("Present"), "present")
        self.assertEqual(_canonicalize_date_string("NOW"), "present")
        self.assertEqual(_canonicalize_date_string("current"), "present")
        self.assertEqual(_canonicalize_date_string("ongoing"), "present")

    def test_empty_and_none(self):
        self.assertIsNone(_canonicalize_date_string(None))
        self.assertIsNone(_canonicalize_date_string(""))
        self.assertIsNone(_canonicalize_date_string("   "))

    def test_garbage(self):
        self.assertIsNone(_canonicalize_date_string("not a date"))
        self.assertIsNone(_canonicalize_date_string("Q1 2020"))

    def test_range_returns_first_half(self):
        self.assertEqual(_canonicalize_date_string("2018 - 2020"), "2018")
        self.assertEqual(_canonicalize_date_string("Jan 2018 – Mar 2020"), "2018-01")
        self.assertEqual(_canonicalize_date_string("2020 to present"), "2020")


class TestSplitDateRange(unittest.TestCase):
    def test_range_with_dash(self):
        self.assertEqual(_split_date_range("2018 - 2020"), ("2018", "2020"))

    def test_range_with_en_dash(self):
        self.assertEqual(
            _split_date_range("Jan 2018 – Mar 2020"), ("2018-01", "2020-03")
        )

    def test_range_to_present(self):
        self.assertEqual(_split_date_range("2020 to present"), ("2020", "present"))

    def test_single_date(self):
        self.assertEqual(_split_date_range("Jan 2020"), ("2020-01", None))

    def test_empty(self):
        self.assertEqual(_split_date_range(None), (None, None))
        self.assertEqual(_split_date_range(""), (None, None))


class TestParseDate(unittest.TestCase):
    def setUp(self):
        self.ingest = IngestResume()

    def test_full_iso(self):
        self.assertEqual(self.ingest.parse_date("2020-05-15"), date(2020, 5, 1))

    def test_month_name(self):
        self.assertEqual(self.ingest.parse_date("Jan 2020"), date(2020, 1, 1))
        self.assertEqual(self.ingest.parse_date("December 1999"), date(1999, 12, 1))

    def test_year_only(self):
        self.assertEqual(self.ingest.parse_date("2020"), date(2020, 1, 1))

    def test_present(self):
        self.assertIsNone(self.ingest.parse_date("present"))

    def test_none_and_empty(self):
        self.assertIsNone(self.ingest.parse_date(None))
        self.assertIsNone(self.ingest.parse_date(""))

    def test_garbage(self):
        self.assertIsNone(self.ingest.parse_date("sometime in 2020"))


class TestExperienceOutValidators(unittest.TestCase):
    """Belt-and-suspenders: even if the LLM emits non-canonical dates, the
    pydantic validator normalizes them before parse_date() runs."""

    def _build(self, **kwargs):
        from job_hunting.lib.services.ingest_resume import ExperienceOut

        defaults = {
            "company": {"name": "Acme"},
            "summary": None,
        }
        defaults.update(kwargs)
        return ExperienceOut(**defaults)

    def test_month_name_normalized(self):
        exp = self._build(start_date="Jan 2020", end_date="Mar 2022")
        self.assertEqual(exp.start_date, "2020-01")
        self.assertEqual(exp.end_date, "2022-03")

    def test_range_in_start_splits_to_both(self):
        exp = self._build(start_date="Jan 2018 – Mar 2020", end_date=None)
        self.assertEqual(exp.start_date, "2018-01")
        self.assertEqual(exp.end_date, "2020-03")

    def test_range_to_present_splits(self):
        exp = self._build(start_date="2020 to present", end_date=None)
        self.assertEqual(exp.start_date, "2020")
        self.assertEqual(exp.end_date, "present")

    def test_present_passthrough(self):
        exp = self._build(start_date="2020", end_date="Present")
        self.assertEqual(exp.end_date, "present")


if __name__ == "__main__":
    unittest.main()
