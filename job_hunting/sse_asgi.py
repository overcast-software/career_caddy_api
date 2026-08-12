"""Standalone ASGI app serving ONLY the SSE event stream.

Option B of `Plans/SSE off sync gunicorn — A-B-C tradeoff` (api
notes.org). The 2026-06-10 racknerd incident: a sync gunicorn worker
holding `/api/v1/events/` open got SIGKILL'd at `GUNICORN_TIMEOUT`
(120s) because sync workers don't reset the arbiter heartbeat between
streaming yields. The long-term fix is to move the one streaming
endpoint off the sync pool entirely onto a small async server with no
arbiter SIGKILL.

This module is that server. It is a minimal Starlette app run under
uvicorn (`uvicorn job_hunting.sse_asgi:app --host 0.0.0.0 --port 8001`
— host + port supplied by the compose command, not hardcoded). The
reverse proxy (caddy) routes `/api/v1/events/` here; everything else,
including `/api/v1/events/token/`, stays on the Django/gunicorn api.

## Routes

- `GET /api/v1/events/` — the SSE stream that moved off gunicorn.
- `GET /healthz` — trivial unauthenticated 200 for the container
  healthcheck. No DB touch.

## Contract reuse — no duplicated auth/LISTEN logic

The token-verify contract (`TimestampSigner(salt="events.sse.token")`,
`max_age=300`), the dedicated psycopg2 LISTEN connection opener, the
per-user forward filter, and the keepalive cadence are all imported
directly from `job_hunting.api.events`. There is exactly one source of
truth for the HMAC token + the `cc_events` channel wiring; this module
only swaps the *transport* (async fan-out from a shared LISTEN
connection in place of the sync generator + `select.select`).

## Process model — ONE shared LISTEN connection per process (CC-229)

The naive design opened a dedicated psycopg2 LISTEN connection PER
stream, held for the whole stream lifetime — so events-service DB
connections scaled with the number of open EventSource tabs and never
cleared. That was a primary driver of the CC-228 Cloud SQL
connection-slot exhaustion.

Instead, a per-process ``_Hub`` (module-level singleton) holds exactly
ONE psycopg2 LISTEN connection on ``cc_events`` and one async dispatcher
task. Each SSE client is an in-memory subscriber: an ``asyncio.Queue``
registered in a per-process registry keyed by ``user_id``. The
dispatcher reads NOTIFYs off the single connection (via
``loop.add_reader`` + ``conn.poll()``) and fans each one out IN-PROCESS
to the matching user's subscriber queues, using the SAME ``user_id``
filter as before. Net: events-service DB connections go from O(open
tabs) → O(processes). Cloud Run runs uvicorn single-process per
instance (deploy locals.tf: no ``--workers``, min/max scale 1..2), so
this is O(instances) — a handful of connections, not one-per-tab.

LISTEN/NOTIFY needs a SESSION-mode connection, so the shared connection
stays a direct psycopg2 connection (``_open_listen_connection``), NOT a
pooled Django one. This is also why the ``events`` service must bypass
any future transaction pooler (CC-230) — a transaction pooler would
break LISTEN. This shared-connection design makes that cheap: one
session-mode connection per process instead of one per tab.

The shared connection is RESILIENT: if it drops, the dispatcher
reconnects + re-LISTENs (with backoff) WITHOUT tearing down the live
subscriber streams. A ``pg_notify`` fired during the reconnect window
is lost (pg_notify is fire-and-forget — the row UPDATE is the source of
truth and the client falls back to polling), but no stream dies.

There is NO duration cap here — uvicorn has no arbiter that SIGKILLs a
long-lived stream, which is the entire point of Option B. Disconnects
are detected via ``request.is_disconnected()`` (checked once per
keepalive cycle) and via ``asyncio.CancelledError`` propagated into the
generator when the ASGI server tears the connection down. Either way
the stream's subscriber is removed from the registry in ``finally`` —
no registry leaks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "job_hunting.settings")
# Load settings (SECRET_KEY, DATABASES) + app registry so the imported
# token signer and ORM-adjacent helpers resolve. Idempotent: a second
# call after the registry is populated returns early.
django.setup()

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import (  # noqa: E402
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import Route  # noqa: E402

# Reuse the EXACT token + LISTEN + filter contract from the sync view.
# No HMAC, no channel name, no psycopg2 wiring is duplicated here.
from job_hunting.api.events import (  # noqa: E402
    _KEEPALIVE_INTERVAL_S,
    _open_listen_connection,
    _should_forward,
    _verify_token,
)

logger = logging.getLogger("job_hunting.api.events")

# Bound on a subscriber's in-memory backlog. If a slow client can't
# drain its queue faster than NOTIFYs arrive, we drop the OLDEST frames
# rather than let the queue grow unbounded (memory) or block the
# dispatcher (head-of-line stalling every other subscriber). SSE
# consumers re-fetch state on reconnect, so a dropped transition is
# recoverable — the row UPDATE remains the source of truth.
_SUBSCRIBER_QUEUE_MAXSIZE = 1000

# Reconnect backoff for the shared LISTEN connection. Small fixed
# backoff — the DB being briefly unreachable shouldn't hammer it, but we
# also want streams to resume promptly once it's back.
_LISTEN_RECONNECT_BACKOFF_S = 1.0

# How long ``subscribe`` waits for the shared connection to establish
# LISTEN before returning anyway. Establishing LISTEN is a single local
# round-trip, so this only elapses when the DB is genuinely unreachable
# — in which case we let the stream come up (keepalives only) rather
# than 500 the request.
_LISTEN_READY_TIMEOUT_S = 5.0


class _Hub:
    """Per-process fan-out hub: ONE shared LISTEN connection, N streams.

    Holds a single psycopg2 LISTEN connection on ``cc_events`` and a
    single dispatcher task. Subscribers register an ``asyncio.Queue``
    keyed by ``user_id``; the dispatcher reads NOTIFYs off the shared
    connection and puts matching payloads onto the right queues.

    The hub is lazily started on the first ``subscribe`` (module import
    runs under ``django.setup()`` with no event loop, so we can't open
    the connection or spawn the task at import time). It stops itself
    when the last subscriber leaves, so an idle process holds zero DB
    connections.
    """

    def __init__(self) -> None:
        # user_id -> set of subscriber queues. A user with multiple open
        # tabs has multiple queues under the same key.
        self._subscribers: dict[int, set[asyncio.Queue[bytes]]] = {}
        self._dispatcher: asyncio.Task | None = None
        # The loop the primitives below are bound to. See
        # ``_bind_running_loop``.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Guards start/stop transitions so concurrent first/last
        # subscribers don't race on the dispatcher task + connection.
        self._lock = asyncio.Lock()
        # Set by the dispatcher once its shared connection has
        # successfully issued ``LISTEN``. ``subscribe`` awaits this so a
        # NOTIFY fired right after a client connects (e.g. the client
        # triggers work then watches for its own completion event) is
        # not lost to a not-yet-established LISTEN — the same
        # LISTEN-before-return guarantee the old per-stream design had.
        self._listening: asyncio.Event = asyncio.Event()

    def _bind_running_loop(self) -> None:
        """Rebuild loop-bound state when the running loop has changed.

        ``_hub`` is a module-level singleton constructed at import, but
        an ``asyncio`` primitive binds to the first loop that *awaits*
        it (3.10+ dropped the explicit ``loop=`` parameter in favour of
        this lazy binding). A process that runs more than one loop over
        its lifetime therefore hits ``RuntimeError: <asyncio.locks.Event
        ...> is bound to a different event loop`` the moment the second
        loop touches ``self._lock`` or ``self._listening``.

        Under uvicorn there is exactly one loop per process, so this is
        a no-op in production. It matters wherever a process opens
        successive loops — most visibly ``asyncio.run()`` per test, which
        is what made ``test_sse_asgi`` fail on a single-process test run
        while passing under a sharded one (BACK-126).

        Everything held here belongs to the previous loop and is
        unusable from the new one — the lock, the event, every
        subscriber ``Queue``, and the dispatcher ``Task`` — so it is
        dropped wholesale rather than migrated. Those subscribers'
        streams died with their loop; there is nothing to preserve.
        """
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._lock = asyncio.Lock()
        self._listening = asyncio.Event()
        self._subscribers = {}
        self._dispatcher = None

    async def subscribe(self, user_id: int) -> asyncio.Queue[bytes]:
        """Register a subscriber queue for ``user_id`` and ensure the
        dispatcher (and its shared LISTEN connection) is running.

        Awaits until the shared connection is actively LISTENing before
        returning, so events fired immediately after subscribe reach the
        channel. If the connection can't be established promptly the
        subscriber is still returned (the stream stays up on keepalives
        and the dispatcher keeps retrying) rather than failing the
        request.
        """
        self._bind_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_MAXSIZE
        )
        async with self._lock:
            self._subscribers.setdefault(user_id, set()).add(queue)
            if self._dispatcher is None or self._dispatcher.done():
                self._listening.clear()
                self._dispatcher = asyncio.ensure_future(self._run())
        try:
            await asyncio.wait_for(self._listening.wait(), _LISTEN_READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("events.hub.listen_not_ready_within_timeout")
        return queue

    async def unsubscribe(
        self, user_id: int, queue: asyncio.Queue[bytes]
    ) -> None:
        """Remove a subscriber queue. When the last subscriber leaves,
        stop the dispatcher so the shared connection is released."""
        # A stream always unsubscribes on the loop it subscribed on, so
        # this is normally an early return. It is here so the ``finally``
        # cleanup in ``_async_event_stream`` can never be the call that
        # raises a cross-loop error while unwinding.
        self._bind_running_loop()
        async with self._lock:
            queues = self._subscribers.get(user_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    del self._subscribers[user_id]
            if not self._subscribers and self._dispatcher is not None:
                self._dispatcher.cancel()
                self._dispatcher = None
                # Next subscribe must wait for a fresh LISTEN, not see a
                # stale ``set`` from the connection we're tearing down.
                self._listening.clear()

    def _fanout(self, payload: dict, raw: str) -> None:
        """Deliver a decoded NOTIFY payload to every matching subscriber.

        Filtered by ``user_id`` via the shared ``_should_forward`` rule.
        A full subscriber queue drops its OLDEST frame to make room —
        never blocks the dispatcher, never grows unbounded.
        """
        for user_id, queues in self._subscribers.items():
            if not _should_forward(payload, user_id):
                continue
            frame = f"data: {raw}\n\n".encode("utf-8")
            for queue in queues:
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Slow consumer: drop the oldest frame, enqueue the
                    # newest. SSE clients recover state on reconnect.
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        pass

    async def _run(self) -> None:
        """Dispatcher loop: hold ONE LISTEN connection, fan NOTIFYs out.

        Resilient to connection drops — reconnects + re-LISTENs without
        disturbing live subscribers. Exits only when cancelled (last
        subscriber gone) via ``asyncio.CancelledError``.
        """
        loop = asyncio.get_running_loop()
        conn = None
        fd = None
        readable = asyncio.Event()

        def _on_readable() -> None:
            readable.set()

        try:
            while True:
                if conn is None:
                    try:
                        conn = _open_listen_connection()
                        fd = conn.fileno()
                        loop.add_reader(fd, _on_readable)
                        # A NOTIFY may have landed between LISTEN and the
                        # reader registration; poll once immediately.
                        readable.set()
                        # LISTEN is live — release any subscribers
                        # blocked in ``subscribe`` waiting for it.
                        self._listening.set()
                        logger.info("events.hub.listen_connected")
                    except Exception:
                        logger.exception(
                            "events.hub.listen_connect_failed"
                        )
                        conn = None
                        fd = None
                        await asyncio.sleep(_LISTEN_RECONNECT_BACKOFF_S)
                        continue

                await readable.wait()
                readable.clear()

                try:
                    conn.poll()
                except Exception:
                    # Connection dropped mid-stream. Tear it down and
                    # loop back to reconnect — subscribers untouched.
                    logger.exception("events.hub.poll_failed_reconnecting")
                    # Not LISTENing until the reconnect re-establishes it.
                    self._listening.clear()
                    if fd is not None:
                        try:
                            loop.remove_reader(fd)
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                    fd = None
                    await asyncio.sleep(_LISTEN_RECONNECT_BACKOFF_S)
                    continue

                while conn.notifies:
                    notif = conn.notifies.pop(0)
                    try:
                        payload = json.loads(notif.payload)
                    except json.JSONDecodeError:
                        logger.warning(
                            "events: dropped malformed payload: %r",
                            notif.payload,
                        )
                        continue
                    self._fanout(payload, notif.payload)
        except asyncio.CancelledError:
            raise
        finally:
            if fd is not None:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.exception("events.hub.cleanup_failed")


# Module-level per-process singleton. One hub → one shared LISTEN
# connection → all streams in this uvicorn worker.
_hub = _Hub()


async def _async_event_stream(
    user_id: int,
    is_disconnected=None,
) -> AsyncGenerator[bytes, None]:
    """Async SSE generator backed by the per-process ``_Hub`` (CC-229).

    Registers an in-memory subscriber queue with the hub (NOT its own DB
    connection) and yields SSE-formatted bytes for ``user_id`` until
    disconnect. NOTIFYs are delivered to the queue by the hub's single
    shared dispatcher; this generator just drains the queue and emits
    idle keep-alives.

    ``is_disconnected`` is an optional awaitable-returning callable
    (``request.is_disconnected``); when it reports True the stream
    breaks cleanly. Tests drive the generator without it and close it
    explicitly.

    Lifecycle: emits ``events.stream.start`` on entry and
    ``events.stream.end user_id=.. duration_ms=.. closed_by=..`` on
    exit (``closed_by`` ∈ {client, error, unknown}). No duration cap.
    The subscriber is ALWAYS removed from the hub registry in
    ``finally`` — no registry leaks on any exit path.
    """
    started_at = time.monotonic()
    closed_by = "unknown"
    logger.info("events.stream.start user_id=%s", user_id)

    queue = await _hub.subscribe(user_id)
    try:
        # Initial comment line establishes the channel — EventSource
        # fires `open` on the client, distinguishing "connected, no
        # events yet" from "still connecting."
        yield b":connected\n\n"

        while True:
            if is_disconnected is not None and await is_disconnected():
                closed_by = "client"
                break

            try:
                frame = await asyncio.wait_for(
                    queue.get(), _KEEPALIVE_INTERVAL_S
                )
            except asyncio.TimeoutError:
                # Idle keep-alive. Comment line — EventSource ignores
                # it; reverse proxies see traffic and don't idle-time.
                yield f": keepalive {int(time.time())}\n\n".encode(
                    "utf-8"
                )
                continue

            yield frame
    except asyncio.CancelledError:
        # uvicorn tore the connection down (client gone). Mirrors the
        # sync view's GeneratorExit path.
        closed_by = "client"
        raise
    except Exception:
        closed_by = "error"
        logger.exception("events.stream.error user_id=%s", user_id)
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "events.stream.end user_id=%s duration_ms=%s closed_by=%s",
            user_id,
            duration_ms,
            closed_by,
        )
        # Always unregister — no registry leak. When this is the last
        # subscriber the hub releases its shared LISTEN connection.
        await _hub.unsubscribe(user_id, queue)


async def events_stream(request: Request):
    """GET /api/v1/events/?token=...

    Token-only auth, identical contract to the sync view. Streams
    ``text/event-stream`` for the user encoded in the signed token.
    """
    token = request.query_params.get("token")
    if not token:
        return JSONResponse(
            {"errors": [{"detail": "token required"}]}, status_code=401
        )

    user_id = _verify_token(token)
    if user_id is None:
        return JSONResponse(
            {"errors": [{"detail": "invalid or expired token"}]},
            status_code=401,
        )

    return StreamingResponse(
        _async_event_stream(
            user_id, is_disconnected=request.is_disconnected
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # caddy/nginx honor this to disable response buffering on
            # the SSE route — without it events sit in the proxy buffer.
            "X-Accel-Buffering": "no",
        },
    )


async def healthz(request: Request):
    """GET /healthz — unauthenticated, no DB. Container healthcheck."""
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/api/v1/events/", events_stream, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)
