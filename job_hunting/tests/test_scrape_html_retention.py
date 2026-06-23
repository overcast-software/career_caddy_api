"""PACA #30: Scrape.html retention — keep successful captures inspectable.

Two halves:

1. ``prune_scrape_html`` keeps the most-recent N *completed* scrapes' html
   per host and nulls html on older completed rows. Failed / non-terminal
   rows are never touched (the failure-path debug-artifact html is the
   operator's diagnostic surface; in-flight rows still have work to do).

2. The ScrapeViewSet PATCH anti-clobber guard: an empty/None ``html`` in a
   PATCH never overwrites a stored capture. This neutralises the
   agents-side ``PersistScrape`` ``html: state.html`` clobber on the
   success path (state.html empty -> a null html PATCH would otherwise
   erase a previously captured DOM).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.lib.tasks import prune_scrape_html
from job_hunting.models import Scrape


User = get_user_model()

HTML = "<html><body>captured</body></html>"


class TestPruneScrapeHtml(TestCase):
    def _mk(self, host, status="completed", html=HTML):
        # Distinct paths keep the rows visually separable; url is not
        # unique-constrained so collisions would be harmless anyway.
        n = Scrape.objects.count() + 1
        return Scrape.objects.create(
            url=f"https://{host}/job/{n}",
            status=status,
            html=html,
        )

    def test_keeps_most_recent_completed_per_host_nulls_older(self):
        a1 = self._mk("greenhouse.io")
        a2 = self._mk("greenhouse.io")
        a3 = self._mk("greenhouse.io")  # newest (highest id)
        b1 = self._mk("allstate.com")

        result = prune_scrape_html(keep_per_host=1)

        for s in (a1, a2, a3, b1):
            s.refresh_from_db()
        self.assertIsNone(a1.html)
        self.assertIsNone(a2.html)
        self.assertEqual(a3.html, HTML)  # newest per host kept
        self.assertEqual(b1.html, HTML)  # only one for its host
        self.assertEqual(result["nulled"], 2)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["hosts"], 2)

    def test_keep_per_host_n_keeps_n_newest(self):
        a1 = self._mk("greenhouse.io")
        a2 = self._mk("greenhouse.io")
        a3 = self._mk("greenhouse.io")

        result = prune_scrape_html(keep_per_host=2)

        for s in (a1, a2, a3):
            s.refresh_from_db()
        self.assertIsNone(a1.html)       # oldest nulled
        self.assertEqual(a2.html, HTML)  # two newest kept
        self.assertEqual(a3.html, HTML)
        self.assertEqual(result["nulled"], 1)
        self.assertEqual(result["kept"], 2)

    def test_failed_rows_retain_html(self):
        f1 = self._mk("dice.com", status="failed")
        f2 = self._mk("dice.com", status="failed")

        result = prune_scrape_html(keep_per_host=1)

        for s in (f1, f2):
            s.refresh_from_db()
        self.assertEqual(f1.html, HTML)
        self.assertEqual(f2.html, HTML)
        self.assertEqual(result["nulled"], 0)

    def test_non_terminal_rows_untouched(self):
        h = self._mk("lever.co", status="hold")
        r = self._mk("lever.co", status="running")

        prune_scrape_html(keep_per_host=1)

        for s in (h, r):
            s.refresh_from_db()
        self.assertEqual(h.html, HTML)
        self.assertEqual(r.html, HTML)

    def test_rows_without_html_are_not_counted(self):
        # A completed row that already has no html shouldn't consume a
        # keep-slot or be reported as kept/nulled.
        self._mk("workday.com", html=None)
        kept_row = self._mk("workday.com")

        result = prune_scrape_html(keep_per_host=1)

        kept_row.refresh_from_db()
        self.assertEqual(kept_row.html, HTML)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["nulled"], 0)

    def test_dry_run_nulls_nothing(self):
        a1 = self._mk("greenhouse.io")
        self._mk("greenhouse.io")

        result = prune_scrape_html(keep_per_host=1, dry_run=True)

        a1.refresh_from_db()
        self.assertEqual(a1.html, HTML)  # untouched
        self.assertEqual(result["would_null"], 1)
        self.assertEqual(result["nulled"], 0)


class TestScrapeHtmlPatchAntiClobber(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="patcher", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.scrape = Scrape.objects.create(
            url="https://greenhouse.io/job/1",
            status="extracting",
            html=HTML,
            created_by=self.user,
        )

    def _patch(self, attributes):
        return self.client.patch(
            f"/api/v1/scrapes/{self.scrape.id}/",
            data={
                "data": {
                    "type": "scrape",
                    "id": str(self.scrape.id),
                    "attributes": attributes,
                }
            },
            format="json",
        )

    def test_patch_null_html_does_not_clobber_stored_html(self):
        resp = self._patch({"html": None, "status": "completed"})
        self.assertEqual(resp.status_code, 200)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.html, HTML)         # capture preserved
        self.assertEqual(self.scrape.status, "completed")  # writable field stuck

    def test_patch_empty_html_does_not_clobber_stored_html(self):
        resp = self._patch({"html": "", "status": "completed"})
        self.assertEqual(resp.status_code, 200)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.html, HTML)

    def test_patch_nonempty_html_replaces(self):
        fresh = "<html><body>fresh</body></html>"
        resp = self._patch({"html": fresh})
        self.assertEqual(resp.status_code, 200)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.html, fresh)
