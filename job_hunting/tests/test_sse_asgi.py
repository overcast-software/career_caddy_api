"""Tests for the standalone SSE ASGI app (``job_hunting.sse_asgi``).

Option B of `Plans/SSE off sync gunicorn — A-B-C tradeoff`: the SSE
stream moved off the sync gunicorn pool onto a small Starlette/uvicorn
app. These tests cover:

- ``/healthz`` → 200 (no auth, no DB).
- missing / invalid token → 401 (before any DB connection opens).
- valid token streams the ``:connected`` preamble with SSE headers.
- a ``pg_notify`` addressed to the user is forwarded as a ``data:``
  line.
- an event addressed to a different user is dropped.

The streaming assertions drive the async generator directly against
the test database, firing real ``pg_notify`` from a dedicated
autocommit connection (the same cross-connection delivery the sync
endpoint relies on). The stream opens its OWN psycopg2 LISTEN
connection — distinct from Django's transaction-wrapped one — so a
committed notify on the channel reaches it just as in production.
"""
from __future__ import annotations

import asyncio
import json

import psycopg2
import psycopg2.extensions
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from starlette.requests import Request
from starlette.testclient import TestClient

from job_hunting.api import events as events_view
from job_hunting.lib import events as events_lib
from job_hunting import sse_asgi


User = get_user_model()

STREAM_URL = "/api/v1/events/"


def _fire_notify(payload: dict) -> None:
    """Emit a pg_notify on ``cc_events`` from a dedicated autocommit
    connection so it commits immediately and crosses to the stream's
    separate LISTEN connection (Django's test-transaction connection
    would never commit it)."""
    db = settings.DATABASES["default"]
    conn = psycopg2.connect(
        dbname=db["NAME"],
        user=db["USER"],
        password=db.get("PASSWORD") or "",
        host=db.get("HOST") or "localhost",
        port=db.get("PORT") or 5432,
    )
    conn.set_isolation_level(
        psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_notify(%s, %s)",
                [events_lib.CHANNEL, json.dumps(payload)],
            )
    finally:
        conn.close()


def _make_get_request(path: str, token: str) -> Request:
    """Minimal ASGI GET request for driving the Starlette handler
    directly. Lets us assert the StreamingResponse header contract
    without an httpx TestClient consuming the unbounded body (which
    would hang — the stream has no duration cap)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": f"token={token}".encode("utf-8"),
        "headers": [],
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


class TestHealthz(TransactionTestCase):
    """GET /healthz is an unauthenticated 200 with no DB access — the
    container healthcheck target."""

    def test_healthz_returns_ok(self):
        with TestClient(sse_asgi.app) as client:
            resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "ok")


class TestStreamAuthGate(TransactionTestCase):
    """GET /api/v1/events/ refuses missing / invalid tokens with 401
    before opening any LISTEN connection. Token is the only credential
    on this route; this app does NOT serve the token-mint endpoint."""

    def test_missing_token_returns_401(self):
        with TestClient(sse_asgi.app) as client:
            resp = client.get(STREAM_URL)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_returns_401(self):
        with TestClient(sse_asgi.app) as client:
            resp = client.get(STREAM_URL, params={"token": "bogus"})
        self.assertEqual(resp.status_code, 401)


class TestStreamConnectedPreamble(TransactionTestCase):
    """A valid token gets a 200 streaming response with SSE headers and
    the ``:connected`` preamble as the first chunk.

    Both facts are asserted WITHOUT consuming the unbounded body: the
    handler is called directly for its StreamingResponse (status + SSE
    headers), and the generator is driven for just the preamble, then
    closed. Iterating the body through an httpx ``TestClient`` and
    ``break``-ing hangs forever — the stream has no duration cap
    (sse_asgi.py), so the early break never tears the in-process
    transport down. The other streaming tests in this module use the
    same direct-drive pattern for exactly this reason.
    """

    def test_valid_token_streams_connected_preamble(self):
        user = User.objects.create_user(username="carol", password="pw")
        token = events_view._sign_token(user.id)

        async def _drive():
            # Header contract — inspect the StreamingResponse the handler
            # builds; never send/consume its (unbounded) body.
            request = _make_get_request(STREAM_URL, token=token)
            resp = await sse_asgi.events_stream(request)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(
                resp.media_type.startswith("text/event-stream")
            )
            self.assertEqual(resp.headers["cache-control"], "no-cache")
            self.assertEqual(resp.headers["x-accel-buffering"], "no")

            # Preamble — drive the generator for just the first chunk,
            # then close it (never enter the uncapped keepalive loop).
            gen = sse_asgi._async_event_stream(user.id)
            try:
                self.assertEqual(
                    await gen.__anext__(), b":connected\n\n"
                )
            finally:
                await gen.aclose()

        asyncio.run(_drive())


class TestStreamForwarding(TransactionTestCase):
    """The async generator forwards events addressed to the connecting
    user and drops events addressed to anyone else."""

    async def _first_data_line(self, user_id, fire_payloads):
        """Drive the generator: read the :connected preamble, fire the
        notifies, then return the first ``data:`` line (skipping any
        keepalive comment lines)."""
        gen = sse_asgi._async_event_stream(user_id)
        try:
            preamble = await gen.__anext__()
            self.assertEqual(preamble, b":connected\n\n")
            for payload in fire_payloads:
                _fire_notify(payload)
            while True:
                chunk = await asyncio.wait_for(
                    gen.__anext__(), timeout=10
                )
                if chunk.startswith(b":"):
                    # comment / keepalive — not a data frame
                    continue
                return chunk
        finally:
            await gen.aclose()

    def test_event_for_user_is_forwarded(self):
        user = User.objects.create_user(username="dave", password="pw")
        payload = {
            "type": "score",
            "id": 11,
            "status": "completed",
            "user_id": user.id,
        }
        chunk = asyncio.run(
            self._first_data_line(user.id, [payload])
        )
        self.assertTrue(chunk.startswith(b"data: "))
        body = json.loads(
            chunk.decode("utf-8")[len("data: ") :].strip()
        )
        self.assertEqual(body["user_id"], user.id)
        self.assertEqual(body["id"], 11)

    def test_event_for_other_user_is_dropped(self):
        user = User.objects.create_user(username="erin", password="pw")
        other = {
            "type": "score",
            "id": 99,
            "status": "completed",
            "user_id": user.id + 1000,
        }
        mine = {
            "type": "score",
            "id": 42,
            "status": "completed",
            "user_id": user.id,
        }
        # Fire the wrong-user event first, then the right one. If the
        # filter were broken the first data frame would carry id=99;
        # asserting it carries id=42 proves the drop.
        chunk = asyncio.run(
            self._first_data_line(user.id, [other, mine])
        )
        body = json.loads(
            chunk.decode("utf-8")[len("data: ") :].strip()
        )
        self.assertEqual(body["id"], 42)
        self.assertEqual(body["user_id"], user.id)
