"""Phase 2 of Plans/Push status updates — SSE replaces polling cap.

Tests the token issuance + verification contract and the SSE endpoint's
auth gating. The streaming loop itself is hard to unit-test (it blocks
in select on a real Postgres LISTEN connection); the connect handshake,
auth, and filter logic are covered here. End-to-end manual verification:
issue token, GET /api/v1/events/?token=..., fire pg_notify, see the
event in curl.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.signing import TimestampSigner
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.api import events as events_view


User = get_user_model()

TOKEN_URL = "/api/v1/events/token/"
STREAM_URL = "/api/v1/events/"


class TestEventsToken(TestCase):
    """POST /api/v1/events/token/ issues a short-lived signed token
    bound to the authenticated user."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_token_endpoint_returns_signed_token(self):
        resp = self.client.post(TOKEN_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("token", body)
        self.assertEqual(body["ttl_seconds"], events_view.TOKEN_TTL_SECONDS)

    def test_token_round_trips_user_id(self):
        resp = self.client.post(TOKEN_URL)
        token = resp.json()["token"]
        self.assertEqual(events_view._verify_token(token), self.user.id)

    def test_token_requires_auth(self):
        unauth = APIClient()
        resp = unauth.post(TOKEN_URL)
        self.assertEqual(resp.status_code, 401)


class TestEventsTokenVerification(TestCase):
    """The _verify_token helper rejects invalid / expired / wrong-salt
    tokens. Subscribers MUST drop these — they're the only auth on the
    SSE channel."""

    def test_bad_signature_returns_none(self):
        self.assertIsNone(events_view._verify_token("not-a-token"))

    def test_wrong_salt_returns_none(self):
        # Sign with a different salt than the events module uses; the
        # verifier should reject because the salt is part of the HMAC.
        signer = TimestampSigner(salt="some.other.context")
        forged = signer.sign("42")
        self.assertIsNone(events_view._verify_token(forged))

    def test_empty_string_returns_none(self):
        self.assertIsNone(events_view._verify_token(""))


class TestEventsStreamAuthGate(TestCase):
    """GET /api/v1/events/ refuses unauthenticated or bad-token requests
    before opening any Postgres LISTEN connection. Token is the only
    credential on this route."""

    def setUp(self):
        self.client = APIClient()

    def test_missing_token_returns_401(self):
        resp = self.client.get(STREAM_URL)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_returns_401(self):
        resp = self.client.get(STREAM_URL + "?token=bogus")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_method_returns_405(self):
        resp = self.client.post(STREAM_URL)
        self.assertEqual(resp.status_code, 405)

    def test_valid_token_opens_stream(self):
        """A correctly-signed token gets a 200 streaming response with
        SSE headers. We don't iterate the body (it would block on the
        real LISTEN connection); just verify the handshake."""
        user = User.objects.create_user(username="bob", password="pw")
        token = events_view._sign_token(user.id)
        # Patch the LISTEN connection opener so the streaming generator
        # doesn't actually hit Postgres if anyone iterates the body.
        with patch(
            "job_hunting.api.events._open_listen_connection",
            return_value=_make_fake_listen_conn(),
        ):
            resp = self.client.get(STREAM_URL + f"?token={token}")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "text/event-stream")
            self.assertEqual(resp["X-Accel-Buffering"], "no")
            self.assertEqual(resp["Cache-Control"], "no-cache")


class TestShouldForward(TestCase):
    """Server-side user filter — events whose payload user_id doesn't
    match the subscriber are dropped before serialization. This IS
    authorization; without it, any token-holder would see every
    user's events."""

    def test_matching_user_id_is_forwarded(self):
        self.assertTrue(
            events_view._should_forward(
                {"type": "score", "id": 1, "user_id": 7}, user_id=7
            )
        )

    def test_non_matching_user_id_is_dropped(self):
        self.assertFalse(
            events_view._should_forward(
                {"type": "score", "id": 1, "user_id": 8}, user_id=7
            )
        )

    def test_null_user_id_is_dropped(self):
        """Early-bail failures emit user_id=None. Those are not
        addressable to any user and should not broadcast."""
        self.assertFalse(
            events_view._should_forward(
                {"type": "score", "id": 1, "user_id": None}, user_id=7
            )
        )

    def test_non_int_user_id_is_dropped(self):
        """Defense against future payload corruption."""
        self.assertFalse(
            events_view._should_forward(
                {"type": "score", "id": 1, "user_id": "alice"},
                user_id=7,
            )
        )


def _make_fake_listen_conn():
    """Return a stand-in psycopg2 connection that's safe for
    StreamingHttpResponse to take + immediately close."""
    from unittest.mock import MagicMock

    conn = MagicMock()
    conn.notifies = []
    # poll() returns immediately; select() in the view loop is what
    # would block — the test doesn't iterate the generator, so we
    # never get there.
    conn.poll.return_value = None
    return conn
