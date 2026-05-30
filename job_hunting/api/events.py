"""SSE endpoint for worker terminal-status transitions.

Phase 2 of `Plans/Push status updates — SSE replaces polling cap for
queue-backed records`. Phase 1 (`job_hunting/lib/events.py`) emits
`pg_notify` on the `cc_events` channel after each terminal UPDATE in
the django-q2 task surfaces. This module subscribes to that channel,
filters notifications by the connecting user's id, and streams them
to the browser as Server-Sent Events.

Two endpoints:

- `POST /api/v1/events/token/` — issues a short-lived (5 min) signed
  token bound to the user, returned in JSON. The token is the only
  credential the SSE endpoint accepts. We don't pass the JWT directly
  in the EventSource URL because (a) JWTs leak into reverse-proxy
  access logs and (b) their 60-min lifetime is far longer than the
  SSE channel needs.

- `GET /api/v1/events/?token=...` — streams `text/event-stream`. The
  connecting client holds the connection open; the server holds a
  dedicated psycopg2 LISTEN connection on `cc_events` and forwards
  matching events. Keep-alive comments fire every 15s to keep
  reverse proxies from idle-timing out.

## Process model

Sync Django view + `StreamingHttpResponse`. Each connection ties up
one gunicorn worker for the lifetime of the SSE channel. At our user
scale this is fine; bump `GUNICORN_WORKERS` if concurrent SSE clients
exceed the pool. Future migration to async + uvicorn worker class
swaps the implementation under the same URL contract.

## psycopg2 LISTEN mechanics

A dedicated psycopg2 connection (NOT borrowed from Django's pool)
runs in autocommit and blocks in `select()` until a notification
arrives or the keep-alive timer fires. Django's pool is unaffected.
On client disconnect, the generator's `finally` closes the
connection.

## Auth choice rationale

Django's `TimestampSigner` (HMAC over `SECRET_KEY`) is sufficient
for the token. We sign just the user id; the verifier rejects
tokens older than 5 minutes via `max_age`. No new dependency, no
JWT-in-URL log leak, no longer-lived credential exposed.

## Filtering

Server-side filter: an event whose payload `user_id` doesn't match
the connecting user is dropped before serialization. Payload events
with `user_id=None` (early-bail failures where the task didn't yet
have a user context) are dropped entirely — they aren't addressable
to any user.
"""

from __future__ import annotations

import json
import logging
import select
import time
from collections.abc import Iterator
from typing import Optional

import psycopg2
import psycopg2.extensions
from django.conf import settings
from django.core.signing import (
    BadSignature,
    SignatureExpired,
    TimestampSigner,
)
from django.http import (
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from job_hunting.lib import events

logger = logging.getLogger(__name__)

# 5 minutes is comfortable for a "client connects shortly after issuing
# the token" handshake. EventSource auto-reconnects with a fresh token
# fetch — Phase 3 frontend logic owns that loop.
TOKEN_TTL_SECONDS = 300

# Separate salt so a token signed for events can't be replayed against
# some hypothetical other TimestampSigner consumer in the codebase.
_TOKEN_SALT = "events.sse.token"

# Idle keep-alive cadence. Reverse proxies (caddy/nginx) typically
# idle-timeout at 30s–60s; 15s gives comfortable margin. SSE comment
# lines start with ':' and are ignored by EventSource clients.
_KEEPALIVE_INTERVAL_S = 15


def _sign_token(user_id: int) -> str:
    return TimestampSigner(salt=_TOKEN_SALT).sign(str(user_id))


def _verify_token(token: str) -> Optional[int]:
    """Return the user id encoded in ``token`` or None if invalid/expired."""
    try:
        raw = TimestampSigner(salt=_TOKEN_SALT).unsign(
            token, max_age=TOKEN_TTL_SECONDS
        )
        return int(raw)
    except (BadSignature, SignatureExpired, ValueError):
        return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def events_token(request) -> Response:
    """POST /api/v1/events/token/

    Returns a short-lived signed token bound to ``request.user``.
    Client passes it via ``?token=...`` on the SSE GET.
    """
    token = _sign_token(request.user.id)
    return Response(
        {"token": token, "ttl_seconds": TOKEN_TTL_SECONDS},
        status=200,
    )


def _open_listen_connection() -> psycopg2.extensions.connection:
    """Open a dedicated psycopg2 connection in autocommit + LISTEN.

    NOT borrowed from Django's pool — LISTEN ties up the connection
    for the entire SSE channel lifetime, and we don't want to starve
    request-handling workers.
    """
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
    with conn.cursor() as cur:
        cur.execute(f"LISTEN {events.CHANNEL};")
    return conn


def _should_forward(payload: dict, user_id: int) -> bool:
    """True iff the event payload addresses this user.

    Drops events with ``user_id=None`` — those are early-bail
    failures whose task didn't yet have a user context, and the SSE
    protocol has no concept of "broadcast to no one." Drops events
    addressed to a different user — server-side authorization.
    """
    p_uid = payload.get("user_id")
    if p_uid is None:
        return False
    try:
        return int(p_uid) == user_id
    except (TypeError, ValueError):
        return False


def _event_stream(user_id: int) -> Iterator[bytes]:
    """Yield SSE-formatted bytes for ``user_id`` until disconnect.

    Format (per the SSE spec):
        data: {"type":"score","id":42,"status":"completed","user_id":7}\\n\\n
        : keepalive 1730000000\\n\\n
    """
    conn = _open_listen_connection()
    # Initial comment line establishes the channel — EventSource fires
    # `open` on the client, lets the frontend distinguish "connected
    # but no events yet" from "still connecting."
    yield b":connected\n\n"
    try:
        while True:
            # select() blocks until the postgres connection has data
            # OR the keep-alive timer fires. The 3-tuple return is
            # (readable, writable, exceptional); empty readable means
            # timeout.
            readable, _, _ = select.select(
                [conn], [], [], _KEEPALIVE_INTERVAL_S
            )
            if not readable:
                # Idle keep-alive. Comment line — EventSource silently
                # ignores; reverse proxies see traffic and don't time out.
                yield f": keepalive {int(time.time())}\n\n".encode("utf-8")
                continue

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
    except GeneratorExit:
        # Client disconnected. Normal path.
        pass
    except Exception:
        logger.exception("events: stream loop terminated")
    finally:
        try:
            conn.close()
        except Exception:
            logger.exception("events: failed to close LISTEN connection")


@csrf_exempt
def events_stream(request) -> HttpResponse:
    """GET /api/v1/events/?token=...

    Streams ``text/event-stream`` for the user encoded in the signed
    token. No JWT — token-only. CSRF exempt because EventSource never
    sends cookies for cross-route GETs (and we don't rely on cookies
    for auth here).
    """
    if request.method != "GET":
        return JsonResponse({"errors": [{"detail": "GET only"}]}, status=405)

    token = request.GET.get("token")
    if not token:
        return JsonResponse(
            {"errors": [{"detail": "token required"}]}, status=401
        )

    user_id = _verify_token(token)
    if user_id is None:
        return JsonResponse(
            {"errors": [{"detail": "invalid or expired token"}]},
            status=401,
        )

    response = StreamingHttpResponse(
        _event_stream(user_id),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    # nginx + caddy both honor this header to disable response buffering
    # on the SSE route. Without it the events sit in the proxy's buffer
    # until enough bytes accumulate or the connection closes — defeating
    # the entire point.
    response["X-Accel-Buffering"] = "no"
    return response
