"""BACK-134 — the chat proxy accepts a `jh_` API key, not just a JWT.

`chat_proxy` is a raw Django view (StreamingHttpResponse has to bypass DRF
content negotiation to stream SSE), so DEFAULT_AUTHENTICATION_CLASSES never
runs and the view authenticates for itself. It used to try JWTAuthentication
and nothing else, which made /api/v1/chat/ the ONLY authenticated endpoint in
the api that rejected a valid `jh_` key — and therefore unreachable from the
browser extension, which holds only that.

What made it expensive to diagnose, and what the middleware test at the bottom
pins: ApiKeyAuthenticationMiddleware has already run by the time the view is
entered and has already set request.user from the key. `request.user` was
authenticated on the exact line that returned 401.

The assertions here are mostly about the FORWARDED CREDENTIAL rather than the
status code. A 200 only proves the caller got in; the extension additionally
needs the raw key to reach the chat service, because chat_server replays it
verbatim on Authorization: Bearer for /api/v1/me/ and every downstream tool
call. Authenticating and then forwarding the wrong thing would pass a
status-code test and fail in production.
"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework_simplejwt.tokens import AccessToken

from job_hunting.api import chat
from job_hunting.models import ApiKey


class _FakeResp:
    status_code = 200

    def iter_lines(self):
        yield 'data: {"type":"done"}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ChatProxyAuthTestCase(TestCase):
    """Shared plumbing: drive the real view and capture what it forwards."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(
            username="back134", email="back134@example.com", password="pw-not-used"
        )

    def _call(self, auth_header=None, extra_headers=None):
        """POST to the view with `auth_header` verbatim.

        Returns (response, captured) where captured["payload"] is the outbound
        body — absent entirely when auth failed, since the view returns before
        opening the stream.
        """
        captured = {}

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def stream(self, method, url, json=None, headers=None):
                captured["payload"] = json
                return _FakeResp()

        headers = dict(extra_headers or {})
        if auth_header is not None:
            headers["HTTP_AUTHORIZATION"] = auth_header

        request = self.factory.post(
            "/api/v1/chat/",
            data=json.dumps({"message": "hi"}),
            content_type="application/json",
            **headers,
        )

        with patch.object(chat, "CHAT_SERVICE_URL", "http://localhost:8031"), patch(
            "httpx.Client", return_value=_FakeClient()
        ):
            response = chat.chat_proxy(request)
            if response.status_code == 200:
                # The httpx call lives inside the streaming generator.
                list(response.streaming_content)

        return response, captured


class TestApiKeyIsAccepted(ChatProxyAuthTestCase):
    def test_valid_key_authenticates(self):
        _, raw_key = ApiKey.generate_key(
            name="extension", user_id=self.user.id, scopes=["read", "write"]
        )
        response, _ = self._call(f"Bearer {raw_key}")
        self.assertEqual(response.status_code, 200)

    def test_the_RAW_KEY_is_what_gets_forwarded(self):
        """The assertion that actually matters.

        chat_server puts this straight onto Authorization: Bearer for
        /api/v1/me/ and every tool call. Forwarding the ApiKey object, its id,
        or the JWT-shaped nothing would all still return 200 here while
        failing every downstream hop.
        """
        _, raw_key = ApiKey.generate_key(
            name="extension", user_id=self.user.id, scopes=["read", "write"]
        )
        _, captured = self._call(f"Bearer {raw_key}")
        self.assertEqual(captured["payload"]["token"], raw_key)
        self.assertTrue(captured["payload"]["token"].startswith("jh_"))

    def test_the_key_resolves_to_its_owner(self):
        """Not just "someone got in" — the right user got in. The chat service
        builds a profile from this identity, so a mix-up would leak one
        account's data into another's conversation."""
        _, raw_key = ApiKey.generate_key(
            name="extension", user_id=self.user.id, scopes=["read", "write"]
        )
        request = self.factory.post(
            "/api/v1/chat/",
            data=json.dumps({"message": "hi"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}",
        )
        user, token = chat._authenticate(request)
        self.assertIsNotNone(user, "the key did not authenticate at all")
        self.assertEqual(user.id, self.user.id)
        self.assertEqual(token, raw_key)


class TestApiKeyRejection(ChatProxyAuthTestCase):
    """Bad keys must 401 — and specifically must NOT 500.

    ApiKeyAuthentication RAISES AuthenticationFailed rather than returning
    None for a key it cannot resolve. In a raw Django view an uncaught DRF
    exception is a 500, so every case here is also a regression test for that
    catch.
    """

    def test_revoked_key_is_401(self):
        key_obj, raw_key = ApiKey.generate_key(
            name="extension", user_id=self.user.id, scopes=["read", "write"]
        )
        key_obj.revoke()
        response, captured = self._call(f"Bearer {raw_key}")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("payload", captured)

    def test_unknown_key_is_401(self):
        response, _ = self._call("Bearer jh_not-a-real-key-at-all")
        self.assertEqual(response.status_code, 401)

    def test_expired_key_is_401(self):
        _, raw_key = ApiKey.generate_key(
            name="extension",
            user_id=self.user.id,
            scopes=["read", "write"],
            expires_days=-1,
        )
        response, _ = self._call(f"Bearer {raw_key}")
        self.assertEqual(response.status_code, 401)

    def test_no_authorization_header_is_401(self):
        response, _ = self._call(None)
        self.assertEqual(response.status_code, 401)

    def test_api_key_on_x_api_key_header_is_401(self):
        """Pins a DELIBERATE narrowing, so it reads as a decision rather than
        an oversight.

        ApiKeyAuthentication also honours X-API-Key and ?api_key=. This view
        accepts the key on Bearer only, because the credential has to be
        FORWARDED and the chat service needs a bearer-shaped one. Accepting it
        here and failing a hop later is worse than a 401. If this test is ever
        made to pass, the forwarding has to change in the same commit.
        """
        _, raw_key = ApiKey.generate_key(
            name="extension", user_id=self.user.id, scopes=["read", "write"]
        )
        response, _ = self._call(None, extra_headers={"HTTP_X_API_KEY": raw_key})
        self.assertEqual(response.status_code, 401)


class TestJwtPathUnchanged(ChatProxyAuthTestCase):
    """The SPA must be untouched by this change."""

    def test_valid_jwt_still_authenticates_and_forwards_itself(self):
        token = str(AccessToken.for_user(self.user))
        response, captured = self._call(f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["payload"]["token"], token)

    def test_garbage_bearer_is_still_401(self):
        response, _ = self._call("Bearer not-a-jwt-and-not-a-jh-key")
        self.assertEqual(response.status_code, 401)


class TestScopeEnforcementHappensBeforeTheView(TestCase):
    """A read-only key gets 403 from middleware, never reaching chat_proxy.

    Documented rather than fought. Worth a test because the symptom — chat
    failing for a key that works fine on GETs — looks like a chat bug and is
    not one. Uses the test client rather than RequestFactory precisely so the
    middleware stack actually runs.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="back134ro", email="back134ro@example.com", password="pw-not-used"
        )

    def test_read_only_key_is_403_from_middleware(self):
        _, raw_key = ApiKey.generate_key(
            name="read-only", user_id=self.user.id, scopes=["read"]
        )
        with patch("httpx.Client") as fake_client:
            response = self.client.post(
                "/api/v1/chat/",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {raw_key}",
            )
        self.assertEqual(response.status_code, 403)
        # The view was never entered, so nothing was proxied.
        fake_client.assert_not_called()

    def test_read_write_key_passes_the_scope_gate(self):
        """The other half of the pair — proves the 403 above is about the
        SCOPE and not about API keys being rejected wholesale."""
        _, raw_key = ApiKey.generate_key(
            name="extension", user_id=self.user.id, scopes=["read", "write"]
        )
        with patch("httpx.Client"):
            response = self.client.post(
                "/api/v1/chat/",
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {raw_key}",
            )
        self.assertEqual(response.status_code, 200)
