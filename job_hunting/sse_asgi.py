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
only swaps the *transport* (async generator + `loop.add_reader` in
place of the sync generator + `select.select`).

## Process model

uvicorn async worker. Each connection holds a dedicated psycopg2
LISTEN connection (NOT borrowed from Django's pool) and an
`asyncio.Event` set by a `loop.add_reader` callback on the connection
fd. There is NO duration cap here — uvicorn has no arbiter that
SIGKILLs a long-lived stream, which is the entire point of Option B.
Disconnects are detected via `request.is_disconnected()` (checked once
per keepalive cycle) and via `asyncio.CancelledError` propagated into
the generator when the ASGI server tears the connection down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator

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


async def _async_event_stream(
    user_id: int,
    is_disconnected=None,
) -> AsyncIterator[bytes]:
    """Async equivalent of the sync view's ``_event_stream``.

    Opens the same dedicated psycopg2 LISTEN connection and yields
    SSE-formatted bytes for ``user_id`` until disconnect. Instead of
    ``select.select`` it registers the connection fd with the running
    loop via ``loop.add_reader`` and waits on an ``asyncio.Event`` so
    the loop is never blocked.

    ``is_disconnected`` is an optional awaitable-returning callable
    (``request.is_disconnected``); when it reports True the stream
    breaks cleanly. Tests drive the generator without it and close it
    explicitly.

    Lifecycle: emits ``events.stream.start`` on entry and
    ``events.stream.end user_id=.. duration_ms=.. closed_by=..`` on
    exit (``closed_by`` ∈ {client, error, unknown}). No duration cap.
    """
    loop = asyncio.get_running_loop()
    conn = _open_listen_connection()
    fd = conn.fileno()
    notified = asyncio.Event()
    started_at = time.monotonic()
    closed_by = "unknown"
    logger.info("events.stream.start user_id=%s", user_id)

    def _on_readable() -> None:
        notified.set()

    try:
        loop.add_reader(fd, _on_readable)

        # Initial comment line establishes the channel — EventSource
        # fires `open` on the client, distinguishing "connected, no
        # events yet" from "still connecting."
        yield b":connected\n\n"

        while True:
            if is_disconnected is not None and await is_disconnected():
                closed_by = "client"
                break

            try:
                await asyncio.wait_for(
                    notified.wait(), _KEEPALIVE_INTERVAL_S
                )
            except asyncio.TimeoutError:
                # Idle keep-alive. Comment line — EventSource ignores
                # it; reverse proxies see traffic and don't idle-time.
                yield f": keepalive {int(time.time())}\n\n".encode(
                    "utf-8"
                )
                continue

            notified.clear()
            conn.poll()
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

                if not _should_forward(payload, user_id):
                    continue

                yield f"data: {notif.payload}\n\n".encode("utf-8")
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
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            logger.exception(
                "events.stream.cleanup_failed user_id=%s", user_id
            )


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
