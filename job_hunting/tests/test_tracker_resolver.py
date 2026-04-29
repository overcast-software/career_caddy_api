"""Unit tests for the tracker URL resolver.

The HTTP layer is mocked — we test the host-detection rules and the
canonicalization that runs over the resolved URL. Integration with
``ScrapeViewSet.create`` lives in test_scrapes_tracker_ingest.
"""
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase
import requests

from job_hunting.lib.tracker_resolver import (
    is_tracker_host,
    resolve_tracker,
)


class IsTrackerHostTests(SimpleTestCase):

    def test_jobot_alerts_subdomain_matches(self):
        self.assertTrue(
            is_tracker_host("https://url9751.alerts.jobot.com/ls/click?upn=abc")
        )

    def test_ziprecruiter_click_subdomain_matches(self):
        self.assertTrue(
            is_tracker_host("https://click.ziprecruiter.com/abc/def")
        )

    def test_linkedin_comm_path_matches(self):
        self.assertTrue(
            is_tracker_host("https://www.linkedin.com/comm/jobs/view/12345")
        )

    def test_linkedin_root_path_does_not_match(self):
        # Only the /comm/ path is treated as a tracker — direct
        # /jobs/view/... should pass through untouched.
        self.assertFalse(
            is_tracker_host("https://www.linkedin.com/jobs/view/12345")
        )

    def test_plain_company_url_does_not_match(self):
        self.assertFalse(is_tracker_host("https://example.com/careers/123"))

    def test_empty_or_invalid_url_returns_false(self):
        self.assertFalse(is_tracker_host(""))
        self.assertFalse(is_tracker_host("not a url"))


class ResolveTrackerTests(SimpleTestCase):

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_happy_path_strips_query_trackers(self, mock_head):
        # Tracker resolves to a destination that itself carries utm_*
        # params. The resolver must canonicalize those out.
        resp = MagicMock()
        resp.url = "https://example.com/jobs/123?utm_source=email&id=456"
        resp.status_code = 200
        mock_head.return_value = resp

        result = resolve_tracker("https://url9751.alerts.jobot.com/ls/click?upn=abc")

        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.resolved_url, "https://example.com/jobs/123?id=456")
        self.assertIsNone(result.error)

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_404_marks_not_ok_and_keeps_status(self, mock_head):
        resp = MagicMock()
        resp.url = "https://example.com/expired"
        resp.status_code = 404
        mock_head.return_value = resp

        result = resolve_tracker("https://click.ziprecruiter.com/dead")

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 404)

    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_request_exception_returns_error(self, mock_head):
        mock_head.side_effect = requests.Timeout("read timeout")

        result = resolve_tracker("https://url9751.alerts.jobot.com/ls/click")

        self.assertFalse(result.ok)
        self.assertIsNone(result.status_code)
        self.assertIn("Timeout", result.error)

    @patch("job_hunting.lib.tracker_resolver.requests.get")
    @patch("job_hunting.lib.tracker_resolver.requests.head")
    def test_405_falls_back_to_get(self, mock_head, mock_get):
        head_resp = MagicMock()
        head_resp.status_code = 405
        head_resp.url = "https://url.greenhouse.io/click/abc"
        mock_head.return_value = head_resp

        get_resp = MagicMock()
        get_resp.url = "https://boards.greenhouse.io/co/123"
        get_resp.status_code = 200
        mock_get.return_value = get_resp

        result = resolve_tracker("https://url.greenhouse.io/click/abc")

        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.resolved_url, "https://boards.greenhouse.io/co/123")
        mock_get.assert_called_once()
