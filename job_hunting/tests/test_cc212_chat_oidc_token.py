"""CC-212 — api chat proxy attaches a Google OIDC ID token to the outbound
request to the IAM-locked internal chat Cloud Run service.

Covers:
  - https Cloud Run target -> Authorization: Bearer <id_token> attached
    (token fetch mocked) and the token is cached across calls
  - localhost/http default -> no Authorization header, no token fetch attempted
  - a token-mint failure degrades gracefully (no header, no raise)
  - the outbound httpx request actually carries the header end-to-end
    through the chat_proxy view
"""

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from job_hunting.api import chat


class TestChatRequestHeaders(TestCase):
    def setUp(self):
        # Isolate the module-level token cache between tests.
        chat._id_token_cache.clear()
        self.addCleanup(chat._id_token_cache.clear)

    def test_https_target_attaches_bearer_token(self):
        with patch.object(chat, "CHAT_SERVICE_URL", "https://chat-abc-uw.a.run.app"), patch(
            "google.oauth2.id_token.fetch_id_token", return_value="tok-123"
        ) as mock_fetch:
            headers = chat._chat_request_headers()

        self.assertEqual(headers["Authorization"], "Bearer tok-123")
        self.assertEqual(headers["Content-Type"], "application/json")
        # Audience must be the base service URL (no /chat path).
        args, _ = mock_fetch.call_args
        self.assertEqual(args[1], "https://chat-abc-uw.a.run.app")

    def test_localhost_default_no_token(self):
        with patch.object(chat, "CHAT_SERVICE_URL", "http://localhost:8031"), patch(
            "google.oauth2.id_token.fetch_id_token"
        ) as mock_fetch:
            headers = chat._chat_request_headers()

        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers, {"Content-Type": "application/json"})
        mock_fetch.assert_not_called()

    def test_http_non_localhost_no_token(self):
        # Non-https target (e.g. a plain-http internal host) must not mint.
        with patch.object(chat, "CHAT_SERVICE_URL", "http://chat.internal:8031"), patch(
            "google.oauth2.id_token.fetch_id_token"
        ) as mock_fetch:
            headers = chat._chat_request_headers()

        self.assertNotIn("Authorization", headers)
        mock_fetch.assert_not_called()

    def test_token_is_cached_across_calls(self):
        with patch.object(chat, "CHAT_SERVICE_URL", "https://chat-abc-uw.a.run.app"), patch(
            "google.oauth2.id_token.fetch_id_token", return_value="tok-123"
        ) as mock_fetch:
            chat._chat_request_headers()
            chat._chat_request_headers()

        # Second call served from cache — token minted only once.
        mock_fetch.assert_called_once()

    def test_mint_failure_degrades_gracefully(self):
        with patch.object(chat, "CHAT_SERVICE_URL", "https://chat-abc-uw.a.run.app"), patch(
            "google.oauth2.id_token.fetch_id_token", side_effect=RuntimeError("no metadata server")
        ):
            headers = chat._chat_request_headers()

        # No header, no raise, no poisoned cache entry.
        self.assertNotIn("Authorization", headers)
        self.assertEqual(chat._id_token_cache, {})


class TestChatProxyOutboundHeader(TestCase):
    """End-to-end through the view: the httpx request carries the header."""

    def setUp(self):
        chat._id_token_cache.clear()
        self.addCleanup(chat._id_token_cache.clear)

    def _run(self, service_url):
        captured = {}

        class _FakeResp:
            status_code = 200

            def iter_lines(self):
                yield 'data: {"type":"done"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def stream(self, method, url, json=None, headers=None):
                captured["headers"] = headers
                captured["url"] = url
                return _FakeResp()

        rf_user = MagicMock(id=7)
        with patch.object(chat, "CHAT_SERVICE_URL", service_url), patch.object(
            chat, "_authenticate", return_value=(rf_user, "jwt-raw-token")
        ), patch("httpx.Client", return_value=_FakeClient()):
            request = MagicMock()
            request.method = "POST"
            request.body = json.dumps({"message": "hi"}).encode()
            response = chat.chat_proxy(request)
            # Drain the streaming generator so stream() is actually invoked.
            list(response.streaming_content)
        return captured

    def test_https_target_sends_authorization(self):
        with patch("google.oauth2.id_token.fetch_id_token", return_value="tok-xyz"):
            captured = self._run("https://chat-abc-uw.a.run.app")
        self.assertEqual(captured["url"], "https://chat-abc-uw.a.run.app/chat")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer tok-xyz")

    def test_localhost_target_no_authorization(self):
        with patch("google.oauth2.id_token.fetch_id_token") as mock_fetch:
            captured = self._run("http://localhost:8031")
        self.assertNotIn("Authorization", captured["headers"])
        mock_fetch.assert_not_called()
