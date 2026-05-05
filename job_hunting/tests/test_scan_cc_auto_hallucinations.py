"""Tests for the scan_cc_auto_hallucinations management command.

The command is read-only — these tests assert (a) similarity helpers
classify boundary cases as expected, (b) the queryset filters honor the
documented args (--source, --include-thin-only, --limit), and (c) the
rendered output flags mismatches and indeterminates while leaving
matches off the human-attention list.

httpx is mocked at the module's `fetch_page_title` boundary — we never
hit real URLs in tests.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from job_hunting.management.commands import scan_cc_auto_hallucinations as scan
from job_hunting.models import Company, JobPost


class TestSimilarityHelpers(TestCase):
    def test_jaccard_empty_returns_zero(self):
        self.assertEqual(scan.jaccard("", "anything"), 0.0)
        self.assertEqual(scan.jaccard("anything", ""), 0.0)

    def test_jaccard_full_overlap_is_one(self):
        self.assertEqual(scan.jaccard("Senior Engineer", "senior engineer"), 1.0)

    def test_jaccard_partial_overlap(self):
        # tokens >2 chars: {senior, engineer} vs {senior, scientist}
        self.assertAlmostEqual(
            scan.jaccard("Senior Engineer", "Senior Scientist"), 1 / 3
        )

    def test_classify_match_above_threshold(self):
        self.assertEqual(
            scan.classify("Senior Backend Engineer",
                          "Senior Backend Engineer | Acme",
                          threshold=0.3),
            "match",
        )

    def test_classify_mismatch_below_threshold(self):
        # jp/1724 case — the smoking gun this command exists to catch.
        self.assertEqual(
            scan.classify(
                "Junior Full Stack Developer at Web Connectivity LLC",
                "Bilingual BD/PM (English/Japanese) — SNBL USA",
                threshold=0.3,
            ),
            "mismatch",
        )

    def test_classify_indeterminate_when_page_title_missing(self):
        self.assertEqual(scan.classify("Anything", None, threshold=0.3),
                         "indeterminate")


class TestCommand(TestCase):
    def setUp(self):
        self.web_co = Company.objects.create(name="Web Connectivity LLC")
        self.snbl = Company.objects.create(name="SNBL USA")
        # The hallucination row from the original incident — stored
        # title is the LLM's wrong guess, link points at SNBL.
        self.bad = JobPost.objects.create(
            title="Junior Full Stack Developer",
            company=self.web_co,
            link="https://www.ziprecruiter.com/jobs/snbl-bilingual-12345",
            source="email",
        )
        # A coherent control row — stored title matches the page.
        self.good = JobPost.objects.create(
            title="Bilingual BD/PM English Japanese",
            company=self.snbl,
            link="https://example.com/snbl-bilingual",
            source="email",
        )
        # A non-cc_auto row — should be filtered out by --source default.
        self.manual = JobPost.objects.create(
            title="Should Not Appear",
            company=self.snbl,
            link="https://example.com/manual",
            source="manual",
        )

    def _fake_fetch(self, url, **_kw):
        if "snbl-bilingual-12345" in url:
            return ("ok", "Bilingual BD/PM (English/Japanese) — SNBL USA", 200)
        if "snbl-bilingual" in url:
            return ("ok", "Bilingual BD PM English Japanese — SNBL", 200)
        if "manual" in url:
            return ("ok", "Should Not Appear", 200)
        return ("http_error", None, 404)

    def test_scan_flags_mismatch_and_excludes_non_cc_auto(self):
        out = StringIO()
        with patch.object(scan, "fetch_page_title", side_effect=self._fake_fetch):
            call_command(
                "scan_cc_auto_hallucinations",
                "--limit", "10",
                "--delay", "0",
                stdout=out,
            )
        text = out.getvalue()
        # Hallucinated row is flagged as mismatch.
        self.assertIn("Mismatches", text)
        self.assertIn(str(self.bad.id), text)
        # The coherent row is NOT under mismatches.
        # (loose check — the good row's id can appear in a "match" count
        # line but must not show up in a mismatch row).
        mismatch_section = text.split("## Mismatches")[1].split("##")[0]
        self.assertNotIn(f"| {self.good.id} ", mismatch_section)
        # The manual-source row is excluded from the scan entirely.
        self.assertNotIn(f"| {self.manual.id} ", text)

    def test_indeterminate_bucket_for_fetch_failures(self):
        def always_fail(url, **_kw):
            return ("fetch_error", "boom", None)

        out = StringIO()
        with patch.object(scan, "fetch_page_title", side_effect=always_fail):
            call_command(
                "scan_cc_auto_hallucinations",
                "--limit", "10",
                "--delay", "0",
                stdout=out,
            )
        text = out.getvalue()
        self.assertIn("Indeterminate", text)
        self.assertIn(str(self.bad.id), text)
        self.assertIn(str(self.good.id), text)

    def test_json_sidecar_written(self):
        import json
        import tempfile

        out = StringIO()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="r+") as tmp:
            with patch.object(scan, "fetch_page_title",
                              side_effect=self._fake_fetch):
                call_command(
                    "scan_cc_auto_hallucinations",
                    "--limit", "10",
                    "--delay", "0",
                    "--json", tmp.name,
                    stdout=out,
                )
            tmp.seek(0)
            payload = json.load(tmp)

        ids = {row["id"] for row in payload}
        self.assertIn(self.bad.id, ids)
        self.assertIn(self.good.id, ids)
        self.assertNotIn(self.manual.id, ids)
        bad_row = next(r for r in payload if r["id"] == self.bad.id)
        self.assertEqual(bad_row["verdict"], "mismatch")
        self.assertEqual(bad_row["fetch_status"], "ok")
        self.assertEqual(bad_row["http_status"], 200)

    def test_include_thin_only_filters_to_empty_descriptions(self):
        # Give one cc_auto row a real description so --include-thin-only
        # excludes it; the bad row stays in.
        self.good.description = "x" * 500
        self.good.save()

        out = StringIO()
        with patch.object(scan, "fetch_page_title", side_effect=self._fake_fetch):
            call_command(
                "scan_cc_auto_hallucinations",
                "--limit", "10",
                "--delay", "0",
                "--include-thin-only",
                stdout=out,
            )
        text = out.getvalue()
        self.assertIn(str(self.bad.id), text)
        self.assertNotIn(f"| {self.good.id} ", text)

    def test_custom_source_arg(self):
        # Override --source to scan only manual posts.
        out = StringIO()
        with patch.object(scan, "fetch_page_title", side_effect=self._fake_fetch):
            call_command(
                "scan_cc_auto_hallucinations",
                "--source", "manual",
                "--limit", "10",
                "--delay", "0",
                stdout=out,
            )
        text = out.getvalue()
        # Both default-source rows excluded, manual row included.
        self.assertNotIn(f"| {self.bad.id} ", text)
        self.assertNotIn(f"| {self.good.id} ", text)
        # manual row's stored title matches the fake page title -> match,
        # so it lands in the count line but not in the mismatch section.
        self.assertIn("match=1", text)
