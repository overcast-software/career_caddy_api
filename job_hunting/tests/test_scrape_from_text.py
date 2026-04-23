"""Tests for POST /api/v1/scrapes/from-text/ — the paste-to-JobPost endpoint."""

from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape

User = get_user_model()


class TestScrapeFromText(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="paster", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_happy_path_returns_202_with_scrape(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme. Remote. $180k-$220k."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertIn("data", body)
        self.assertEqual(body["data"]["type"], "scrape")

        scrape = Scrape.objects.get(pk=int(body["data"]["id"]))
        self.assertEqual(scrape.status, "pending")
        self.assertEqual(scrape.url, None)
        self.assertIn("Senior Engineer", scrape.job_content)
        self.assertEqual(scrape.created_by, self.user)

        mock_parse.assert_called_once()
        args, kwargs = mock_parse.call_args
        self.assertEqual(args[0], scrape.id)
        self.assertEqual(kwargs.get("user_id"), self.user.id)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_optional_link_stored(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Some job content",
                "link": "https://example.com/jobs/1",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.url, "https://example.com/jobs/1")
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_empty_text_rejected(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_missing_text_rejected(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_parse.assert_not_called()

    def test_auth_required(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "content"},
            format="json",
        )
        self.assertIn(resp.status_code, (401, 403))


class TestScrapeFromTextDuplicateLink(TestCase):
    """Inbox bug: pasting a URL whose JobPost already exists used to create
    a Scrape and run the full LLM extraction before the dedup check fired —
    a wasted agent call. Now we short-circuit with 409 and let the user
    open the existing post or opt-in to a re-parse with force=true.

    Stubs (thin/empty description) still pass through so parse_scrape can
    upgrade them in place.
    """

    DUPLICATE_LINK = "https://talent.toptal.com/portal/job/VjEtSm9iLTQ5Mzg4OA"
    LONG_DESC = " ".join(["word"] * 200)

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="paster2", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Toptal")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_non_stub_duplicate_returns_409_without_creating_scrape(self, mock_parse):
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal", "link": self.DUPLICATE_LINK},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        body = resp.json()
        err = body["errors"][0]
        self.assertEqual(err["code"], "duplicate_job_post")
        self.assertEqual(err["meta"]["job_post_id"], existing.id)
        self.assertEqual(err["meta"]["link"], self.DUPLICATE_LINK)
        self.assertEqual(err["meta"]["title"], "Senior Engineer")
        self.assertEqual(err["meta"]["company_name"], "Toptal")
        # No Scrape created, no agent call.
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_stub_duplicate_passes_through_for_upgrade(self, mock_parse):
        """Thin/empty description = stub. parse_scrape's existing
        updated_stub branch should still get a chance to fill it in."""
        JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description="",  # stub
            link=self.DUPLICATE_LINK,
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal", "link": self.DUPLICATE_LINK},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(Scrape.objects.count(), 1)
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_force_true_overrides_dedup(self, mock_parse):
        """User explicitly opts in to re-parse over an existing post."""
        JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Toptal",
                "link": self.DUPLICATE_LINK,
                "force": True,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(Scrape.objects.count(), 1)
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_no_link_skips_dedup_check(self, mock_parse):
        """Paste without a URL — never had a chance to match anything."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_parse.assert_called_once()
