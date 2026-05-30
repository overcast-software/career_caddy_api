"""Phase 1 of Plans/Push status updates — SSE replaces polling cap.

Pins the pg_notify emission contract for `job_hunting.lib.events.notify`.
Subscribers (Phase 2's SSE endpoint) LISTEN on the ``cc_events`` channel
and depend on the payload shape pinned here.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase

from job_hunting.lib import events


class TestEventsNotify(TestCase):
    def test_pg_notify_called_with_channel_and_payload(self):
        """notify() emits a single pg_notify call on the cc_events
        channel with a JSON payload containing type / id / status /
        user_id. The exact wire shape that downstream subscribers
        consume."""
        with patch("job_hunting.lib.events.connection") as mock_conn:
            cursor = mock_conn.cursor.return_value.__enter__.return_value
            events.notify("score", 42, "completed", user_id=7)

            cursor.execute.assert_called_once()
            sql, params = cursor.execute.call_args.args
            self.assertEqual(sql, "SELECT pg_notify(%s, %s)")
            self.assertEqual(params[0], "cc_events")
            payload = json.loads(params[1])
            self.assertEqual(
                payload,
                {
                    "type": "score",
                    "id": 42,
                    "status": "completed",
                    "user_id": 7,
                },
            )

    def test_none_user_id_serializes(self):
        """user_id=None is a valid payload — early-bail failures may
        not have a user context. Don't drop the event; let the SSE
        endpoint decide what to do with it."""
        with patch("job_hunting.lib.events.connection") as mock_conn:
            cursor = mock_conn.cursor.return_value.__enter__.return_value
            events.notify("answer", 99, "failed", user_id=None)

            sql, params = cursor.execute.call_args.args
            payload = json.loads(params[1])
            self.assertIsNone(payload["user_id"])

    def test_exception_is_swallowed(self):
        """A pg_notify failure must never crash the calling task. The
        notify call is best-effort — the underlying row UPDATE is the
        source of truth either way."""
        with patch("job_hunting.lib.events.connection") as mock_conn:
            cursor = mock_conn.cursor.return_value.__enter__.return_value
            cursor.execute.side_effect = RuntimeError("postgres exploded")

            # Should not raise.
            events.notify("score", 1, "completed", user_id=1)

    def test_channel_name_pinned(self):
        """Subscribers LISTEN on a specific channel name. Pin it here so
        a careless rename breaks the test, not silently the SSE
        endpoint."""
        self.assertEqual(events.CHANNEL, "cc_events")
