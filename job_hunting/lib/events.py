"""Fire-and-forget event emission via Postgres pg_notify.

Phase 1 of `Plans/Push status updates — SSE replaces polling cap for
queue-backed records`. Each django-q2 task in `lib/tasks.py` calls
`notify()` after a terminal status UPDATE so a downstream SSE endpoint
(Phase 2) can fan out the transition to subscribed clients without
either side polling the DB.

Channel: ``cc_events``. Payload: JSON with ``type`` / ``id`` /
``status`` / ``user_id``. Subscribers ``LISTEN cc_events`` and filter
by ``user_id`` server-side before forwarding to the client.

Design choices:

- **pg_notify is fire-and-forget.** If no subscriber is listening, the
  notification is dropped. That's fine — clients fall back to the
  existing polling path on EventSource disconnect, and the row UPDATE
  is the source of truth either way. We are NOT building an event log
  here; durable delivery is out of scope for Phase 1.
- **Synchronous cursor.execute is fine.** pg_notify returns
  immediately; the only cost is the round-trip to Postgres, which the
  worker already pays for the row UPDATE. No locking, no transactions
  to worry about.
- **No exception escape.** A logging failure during pg_notify must
  never crash the task. The notify call is best-effort and exceptions
  are swallowed with logger.exception so django_q.Failure doesn't
  light up on a transient Postgres hiccup that has nothing to do with
  the actual work.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from django.db import connection

logger = logging.getLogger(__name__)

CHANNEL = "cc_events"

# Event types currently emitted. Listed here as a single source of
# truth so Phase 2's SSE endpoint can validate / filter, and so a
# future contributor sees what payloads to expect on the wire.
EventType = Literal[
    "score",
    "summary",
    "cover_letter",
    "answer",
    "resume",
    "scrape",
]


def notify(
    event_type: EventType,
    record_id: int,
    status: str,
    user_id: int | None = None,
) -> None:
    """Emit a pg_notify on the cc_events channel.

    Best-effort. Never raises — exceptions are logged and swallowed
    so a notification failure cannot fail the calling task.
    """
    payload = {
        "type": event_type,
        "id": record_id,
        "status": status,
        "user_id": user_id,
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_notify(%s, %s)",
                [CHANNEL, json.dumps(payload)],
            )
    except Exception:
        logger.exception(
            "events.notify failed for type=%s id=%s status=%s",
            event_type,
            record_id,
            status,
        )
