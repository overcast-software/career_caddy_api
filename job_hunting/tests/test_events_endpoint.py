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


class TestEventStreamDisconnectAndLifecycle(TestCase):
    """The 2026-06-09/06-10 gunicorn OOM cascades were rooted in this
    generator failing to notice a client disconnect, then accreting
    notifies + yielded bytes until the worker hit the memory cap.

    The fix bounds ``select.select`` at ``_KEEPALIVE_INTERVAL_S`` (so
    the loop revisits a yield site at least that often) and translates
    a ``BrokenPipeError`` / ``ConnectionResetError`` raised by the WSGI
    server's write into a clean exit with structured lifecycle logs.

    These tests drive ``_event_stream`` as a raw generator — bypassing
    StreamingHttpResponse — so we can inject the disconnect via
    ``gen.throw(...)`` exactly the way gunicorn does in production.
    """

    def setUp(self):
        # The generator opens a real psycopg2 connection by default;
        # patch it to a MagicMock for every test in this class.
        self.fake_conn = _make_fake_listen_conn()
        self.conn_patcher = patch(
            "job_hunting.api.events._open_listen_connection",
            return_value=self.fake_conn,
        )
        self.conn_patcher.start()
        self.addCleanup(self.conn_patcher.stop)

    def test_select_timeout_yields_keepalive_then_loops(self):
        """When ``select.select`` returns no readable connections (the
        keepalive timeout path), the generator MUST yield a keepalive
        comment line and continue. Without the bounded timeout the loop
        would block indefinitely and the disconnect-detection write
        below would never happen."""
        with patch(
            "job_hunting.api.events.select.select",
            return_value=([], [], []),
        ) as mock_select:
            gen = events_view._event_stream(user_id=7)
            # First yield is the :connected handshake.
            self.assertEqual(next(gen), b":connected\n\n")
            # Second yield is the keepalive (select returned empty).
            chunk = next(gen)
            self.assertTrue(chunk.startswith(b": keepalive "))
            # Verify the bounded timeout was passed to select.
            args, _kwargs = mock_select.call_args
            self.assertEqual(
                args[3], events_view._KEEPALIVE_INTERVAL_S
            )
            # Drain so the finally runs.
            gen.close()

    def test_broken_pipe_on_keepalive_exits_cleanly(self):
        """When the WSGI server raises ``BrokenPipeError`` on a write
        (the canonical signal that the client has disconnected), the
        generator MUST translate it to ``_ClientDisconnected``, log
        a structured ``events.stream.end`` line with ``closed_by=client``,
        close the LISTEN connection, and raise ``StopIteration``."""
        with patch(
            "job_hunting.api.events.select.select",
            return_value=([], [], []),
        ):
            gen = events_view._event_stream(user_id=7)
            # Drive past the :connected handshake.
            self.assertEqual(next(gen), b":connected\n\n")
            with self.assertLogs(
                "job_hunting.api.events", level="INFO"
            ) as captured:
                with self.assertRaises(StopIteration):
                    # gen.throw injects the exception at the yield site,
                    # exactly mirroring how WSGI signals a dead socket
                    # during a write.
                    gen.throw(BrokenPipeError())
            joined = "\n".join(captured.output)
            self.assertIn("events.stream.end", joined)
            self.assertIn("closed_by=client", joined)
            self.assertIn("user_id=7", joined)
            self.assertIn("duration_ms=", joined)
            # The LISTEN connection MUST be released in the finally
            # block — leaking these is what caused the cascade.
            self.fake_conn.close.assert_called_once()

    def test_connection_reset_on_data_yield_exits_cleanly(self):
        """Same contract as broken-pipe, but at the data yield site
        (after a pg notify) and with ``ConnectionResetError`` — the
        other kernel-level signal we may see from gunicorn's write."""
        with patch(
            "job_hunting.api.events.select.select",
            return_value=([], [], []),
        ):
            gen = events_view._event_stream(user_id=7)
            next(gen)  # :connected
            with self.assertLogs(
                "job_hunting.api.events", level="INFO"
            ) as captured:
                with self.assertRaises(StopIteration):
                    gen.throw(ConnectionResetError())
            self.assertIn(
                "closed_by=client",
                "\n".join(captured.output),
            )
            self.fake_conn.close.assert_called_once()

    def test_generator_exit_logs_lifecycle_end(self):
        """``GeneratorExit`` is the gunicorn-noticed-disconnect path
        (vs. the BrokenPipe path which is yield-side detection). Both
        must produce a clean ``events.stream.end`` line — that's how
        Doug sees session duration in logfire."""
        with patch(
            "job_hunting.api.events.select.select",
            return_value=([], [], []),
        ):
            gen = events_view._event_stream(user_id=42)
            next(gen)
            with self.assertLogs(
                "job_hunting.api.events", level="INFO"
            ) as captured:
                gen.close()
            joined = "\n".join(captured.output)
            self.assertIn("events.stream.end", joined)
            self.assertIn("closed_by=client", joined)
            self.assertIn("user_id=42", joined)
            self.fake_conn.close.assert_called_once()

    def test_start_log_line_fires_on_entry(self):
        """Symmetric to the end-log line — the ``events.stream.start``
        INFO line lets us pair start/end events in logfire and reason
        about session counts."""
        with patch(
            "job_hunting.api.events.select.select",
            return_value=([], [], []),
        ):
            with self.assertLogs(
                "job_hunting.api.events", level="INFO"
            ) as captured:
                gen = events_view._event_stream(user_id=99)
                next(gen)  # force the generator to enter
                gen.close()
            joined = "\n".join(captured.output)
            self.assertIn("events.stream.start", joined)
            self.assertIn("user_id=99", joined)

    def test_unexpected_exception_logs_error_and_cleans_up(self):
        """An unexpected exception inside the loop (NOT a disconnect)
        MUST be logged at ERROR level (so logfire surfaces it) AND
        still release the LISTEN connection. Without the finally, the
        leak that caused the OOM stays."""
        with patch(
            "job_hunting.api.events.select.select",
            side_effect=RuntimeError("simulated kernel error"),
        ):
            gen = events_view._event_stream(user_id=7)
            with self.assertLogs(
                "job_hunting.api.events", level="INFO"
            ) as captured:
                # First next() runs the :connected yield. The next one
                # enters the while loop and trips the simulated error.
                with self.assertRaises(StopIteration):
                    next(gen)
                    next(gen)
            joined = "\n".join(captured.output)
            self.assertIn("events.stream.error", joined)
            self.assertIn("events.stream.end", joined)
            self.assertIn("closed_by=error", joined)
            self.fake_conn.close.assert_called_once()
