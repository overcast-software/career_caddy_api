"""Tests for POST /api/v1/scrapes/from-text/ — the paste-to-JobPost endpoint."""

from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Scrape

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
