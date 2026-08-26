"""Tests for the per-process shared LISTEN hub (``sse_asgi._Hub``, CC-229).

The naive SSE design opened one dedicated psycopg2 LISTEN connection per
stream, held for the whole stream lifetime — so events-service DB
connections scaled with open EventSource tabs (a driver of the CC-228
Cloud SQL connection-slot exhaustion). The fix: ONE shared LISTEN
connection per process, with NOTIFYs fanned out in-process to per-stream
``asyncio.Queue`` subscribers keyed by ``user_id``.

These tests prove the load-bearing invariants:

- Multiple concurrent subscribers (same AND different ``user_id``) all
  receive the right NOTIFYs from a SINGLE LISTEN connection — asserted
  by counting how many times ``_open_listen_connection`` is called
  across N concurrent streams (exactly one).
- A disconnect removes the subscriber from the registry (no leak) and,
  when it's the last subscriber, releases the shared connection.
- The shared connection reconnects + re-LISTENs after a dropped
  connection without tearing down live streams.

Streaming assertions fire real ``pg_notify`` from a dedicated autocommit
connection (the cross-connection delivery production relies on) and read
frames off the async generators.
"""
from __future__ import annotations

import asyncio
import json
from unittest import mock

import psycopg2
import psycopg2.extensions
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from job_hunting import sse_asgi
from job_hunting.api import events as events_view
from job_hunting.lib import events as events_lib


User = get_user_model()


def _fire_notify(payload: dict) -> None:
    """Emit a pg_notify on ``cc_events`` from a dedicated autocommit
    connection so it commits immediately and crosses to the hub's
    separate LISTEN connection.

    Connects through ``_listen_connection_params`` for the same reason
    production does — a config shape the LISTEN connection can't reach
    should break this helper too, not quietly work against some other
    database (CC-252)."""
    conn = psycopg2.connect(**events_view._listen_connection_params())
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_notify(%s, %s)",
                [events_lib.CHANNEL, json.dumps(payload)],
            )
    finally:
        conn.close()


async def _next_data_frame(gen, timeout=10):
    """Drive ``gen`` past keepalive comments and return the next
    ``data:`` frame (decoded JSON body)."""
    while True:
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
        if chunk.startswith(b":"):
            continue  # :connected preamble or : keepalive comment
        assert chunk.startswith(b"data: ")
        return json.loads(chunk.decode("utf-8")[len("data: ") :].strip())


class TestSharedListenConnection(TransactionTestCase):
    """N concurrent subscribers share exactly ONE LISTEN connection, and
    each still gets only its own user's events."""

    def setUp(self):
        # Fresh hub per test — module singleton would otherwise leak
        # state (a running dispatcher / registered subscribers) between
        # tests.
        sse_asgi._hub = sse_asgi._Hub()

    def test_two_users_one_connection_correct_routing(self):
        alice = User.objects.create_user(username="alice", password="pw")
        bob = User.objects.create_user(username="bob", password="pw")

        open_calls = {"n": 0}
        real_open = sse_asgi._open_listen_connection

        def _counting_open():
            open_calls["n"] += 1
            return real_open()

        async def _drive():
            with mock.patch.object(
                sse_asgi, "_open_listen_connection", _counting_open
            ):
                a_gen = sse_asgi._async_event_stream(alice.id)
                b_gen = sse_asgi._async_event_stream(bob.id)
                try:
                    # Both preambles — both subscribers registered, one
                    # shared dispatcher started.
                    self.assertEqual(await a_gen.__anext__(), b":connected\n\n")
                    self.assertEqual(await b_gen.__anext__(), b":connected\n\n")
                    # Let the dispatcher spin up + connect.
                    await asyncio.sleep(0.2)

                    _fire_notify(
                        {"type": "score", "id": 1, "status": "completed",
                         "user_id": alice.id}
                    )
                    _fire_notify(
                        {"type": "score", "id": 2, "status": "completed",
                         "user_id": bob.id}
                    )

                    a_body = await _next_data_frame(a_gen)
                    b_body = await _next_data_frame(b_gen)
                    self.assertEqual(a_body["id"], 1)
                    self.assertEqual(a_body["user_id"], alice.id)
                    self.assertEqual(b_body["id"], 2)
                    self.assertEqual(b_body["user_id"], bob.id)
                finally:
                    await a_gen.aclose()
                    await b_gen.aclose()

            # THE core CC-229 invariant: two streams, ONE LISTEN
            # connection opened.
            self.assertEqual(open_calls["n"], 1)

        asyncio.run(_drive())

    def test_two_tabs_same_user_both_receive(self):
        """Two open tabs for the same user (two subscriber queues under
        one key) both receive the event from the single connection."""
        carol = User.objects.create_user(username="carol", password="pw")

        open_calls = {"n": 0}
        real_open = sse_asgi._open_listen_connection

        def _counting_open():
            open_calls["n"] += 1
            return real_open()

        async def _drive():
            with mock.patch.object(
                sse_asgi, "_open_listen_connection", _counting_open
            ):
                tab1 = sse_asgi._async_event_stream(carol.id)
                tab2 = sse_asgi._async_event_stream(carol.id)
                try:
                    self.assertEqual(await tab1.__anext__(), b":connected\n\n")
                    self.assertEqual(await tab2.__anext__(), b":connected\n\n")
                    await asyncio.sleep(0.2)

                    _fire_notify(
                        {"type": "score", "id": 7, "status": "completed",
                         "user_id": carol.id}
                    )

                    b1 = await _next_data_frame(tab1)
                    b2 = await _next_data_frame(tab2)
                    self.assertEqual(b1["id"], 7)
                    self.assertEqual(b2["id"], 7)
                finally:
                    await tab1.aclose()
                    await tab2.aclose()

            self.assertEqual(open_calls["n"], 1)

        asyncio.run(_drive())


class TestSubscriberRegistryCleanup(TransactionTestCase):
    """A disconnect removes the subscriber from the registry; the last
    disconnect stops the dispatcher (releasing the shared connection)."""

    def setUp(self):
        sse_asgi._hub = sse_asgi._Hub()

    def test_disconnect_unregisters_and_releases_connection(self):
        dave = User.objects.create_user(username="dave", password="pw")

        async def _drive():
            hub = sse_asgi._hub
            gen = sse_asgi._async_event_stream(dave.id)
            self.assertEqual(await gen.__anext__(), b":connected\n\n")
            await asyncio.sleep(0.1)

            # Registered + dispatcher running.
            self.assertIn(dave.id, hub._subscribers)
            self.assertEqual(len(hub._subscribers[dave.id]), 1)
            self.assertIsNotNone(hub._dispatcher)

            await gen.aclose()
            # Let unsubscribe + dispatcher-cancel settle.
            await asyncio.sleep(0.1)

            # No registry leak; shared connection released (dispatcher
            # torn down on last subscriber).
            self.assertNotIn(dave.id, hub._subscribers)
            self.assertIsNone(hub._dispatcher)

        asyncio.run(_drive())

    def test_one_of_two_disconnects_keeps_connection(self):
        """When one of two subscribers disconnects, the other keeps the
        shared connection alive."""
        erin = User.objects.create_user(username="erin", password="pw")

        async def _drive():
            hub = sse_asgi._hub
            g1 = sse_asgi._async_event_stream(erin.id)
            g2 = sse_asgi._async_event_stream(erin.id)
            self.assertEqual(await g1.__anext__(), b":connected\n\n")
            self.assertEqual(await g2.__anext__(), b":connected\n\n")
            await asyncio.sleep(0.1)
            self.assertEqual(len(hub._subscribers[erin.id]), 2)
            dispatcher = hub._dispatcher
            self.assertIsNotNone(dispatcher)

            await g1.aclose()
            await asyncio.sleep(0.1)

            # One left — same dispatcher, connection NOT released.
            self.assertEqual(len(hub._subscribers[erin.id]), 1)
            self.assertIs(hub._dispatcher, dispatcher)
            self.assertFalse(dispatcher.done())

            await g2.aclose()

        asyncio.run(_drive())


class _PollFailsOnceConnection:
    """Wraps a real psycopg2 LISTEN connection but makes the FIRST
    ``poll()`` after wrapping raise, modelling a mid-stream connection
    drop. Everything else delegates to the real connection so the
    dispatcher's ``fileno`` / ``notifies`` / ``close`` all still work.

    The wrapper is handed out by a patched ``_open_listen_connection``
    for the FIRST connection only; the reconnect opens a real one, so
    delivery resumes on the same live stream — proving the in-``_run``
    reconnect path (poll raises → close → reopen → re-LISTEN) without
    disturbing subscribers.
    """

    def __init__(self, real):
        self._real = real
        self._poll_should_fail = True

    def fileno(self):
        return self._real.fileno()

    def poll(self):
        if self._poll_should_fail:
            self._poll_should_fail = False
            raise psycopg2.OperationalError("simulated connection drop")
        return self._real.poll()

    @property
    def notifies(self):
        return self._real.notifies

    def close(self):
        return self._real.close()


class TestSharedConnectionReconnect(TransactionTestCase):
    """The shared LISTEN connection reconnects + re-LISTENs after a drop
    without tearing down the live stream."""

    def setUp(self):
        sse_asgi._hub = sse_asgi._Hub()

    def test_reconnect_after_dropped_connection(self):
        frank = User.objects.create_user(username="frank", password="pw")

        open_calls = {"n": 0}
        real_open = sse_asgi._open_listen_connection

        def _flaky_then_real_open():
            open_calls["n"] += 1
            conn = real_open()
            if open_calls["n"] == 1:
                # First connection: poll() will raise once → forces the
                # dispatcher's in-loop reconnect branch.
                return _PollFailsOnceConnection(conn)
            return conn

        async def _drive():
            with mock.patch.object(
                sse_asgi, "_open_listen_connection", _flaky_then_real_open
            ):
                gen = sse_asgi._async_event_stream(frank.id)
                try:
                    self.assertEqual(await gen.__anext__(), b":connected\n\n")
                    # The dispatcher's connect-time poll hits the wrapped
                    # connection whose first poll() raises → it closes
                    # the dropped conn, reopens a REAL one, re-LISTENs.
                    # Wait for that reconnect (a second _open call) before
                    # firing, so the NOTIFY lands on the re-LISTENed
                    # connection rather than in the reconnect window
                    # (pg_notify is fire-and-forget).
                    deadline = asyncio.get_event_loop().time() + 5
                    while open_calls["n"] < 2:
                        if asyncio.get_event_loop().time() > deadline:
                            self.fail("shared connection did not reconnect")
                        await asyncio.sleep(0.05)

                    _fire_notify(
                        {"type": "score", "id": 55, "status": "completed",
                         "user_id": frank.id}
                    )
                    # Delivery survives the drop on the SAME live stream.
                    body = await _next_data_frame(gen, timeout=10)
                    self.assertEqual(body["id"], 55)
                finally:
                    await gen.aclose()

        asyncio.run(_drive())
