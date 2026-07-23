"""CC-206 — federation dispatch + inbox enqueue-contract (bucket-2).

Both ActivityPub async sites move from django-q2 ``async_task`` to the unified
``enqueue(kind, **payload)`` producer:

- OUTBOUND dispatch (``_schedule_dispatch_task``): fire-now →
  ``enqueue('federation_dispatch', federation_activity_id=...)``; future-dated
  → the same with ``run_after=when`` (Cloud Tasks schedule_time / Job.run_after
  — the native delayed-dispatch primitive replacing the old one-shot Schedule
  row). The retry state machine (FederationActivity.retry_count/next_attempt_at
  + sweep_pending_dispatches) is unchanged and NOT retested here.

- INBOUND inbox (``enqueue_inbound_activity``): the raw request body is bytes,
  so it rides the JSON payload base64-encoded; ``run_inbound_activity_task``
  base64-decodes it back to the exact bytes before the existing
  ``process_inbound_activity`` worker verifies the signature.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from job_hunting.lib import federation_dispatch, federation_inbox


class TestDispatchEnqueueContract(TestCase):
    def test_fire_now_enqueues_federation_dispatch_no_delay(self):
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            federation_dispatch._schedule_dispatch_task(42)
        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "federation_dispatch")
        self.assertEqual(kwargs["federation_activity_id"], 42)
        self.assertIsNone(kwargs["run_after"])

    def test_past_when_is_treated_as_fire_now(self):
        past = timezone.now() - timezone.timedelta(minutes=5)
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            federation_dispatch._schedule_dispatch_task(7, when=past)
        _, kwargs = mock_enqueue.call_args
        self.assertIsNone(kwargs["run_after"])

    def test_future_when_passes_run_after(self):
        future = timezone.now() + timezone.timedelta(minutes=30)
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            federation_dispatch._schedule_dispatch_task(9, when=future)
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "federation_dispatch")
        self.assertEqual(kwargs["federation_activity_id"], 9)
        self.assertEqual(kwargs["run_after"], future)


@override_settings(ACTIVITYPUB_INBOX_DISPATCH_SYNC=False)
class TestInboxEnqueueContract(TestCase):
    def test_body_base64_round_trips_exactly(self):
        body = b'{"type":"Follow","actor":"https://peer.example/u/x"}'
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            federation_inbox.enqueue_inbound_activity(
                actor_kind="person",
                identifier="dough",
                method="POST",
                path="/actors/dough/inbox",
                headers={"Signature": "keyId=..."},
                body=body,
            )
        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "federation_inbox")
        # The payload carries base64, NOT raw bytes (JSON-serializable), and it
        # decodes back to the exact request bytes.
        import base64

        self.assertNotIn("body", kwargs)
        self.assertEqual(base64.b64decode(kwargs["body_b64"]), body)
        self.assertEqual(kwargs["headers"]["Signature"], "keyId=...")

    def test_str_body_is_encoded_before_base64(self):
        # The edge normally passes bytes; a str body must still round-trip.
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            federation_inbox.enqueue_inbound_activity(
                actor_kind="company",
                identifier="acme",
                method="POST",
                path="/companies/acme/inbox",
                headers={},
                body='{"type":"Delete"}',
            )
        import base64

        _, kwargs = mock_enqueue.call_args
        self.assertEqual(
            base64.b64decode(kwargs["body_b64"]), b'{"type":"Delete"}'
        )

    def test_task_wrapper_decodes_and_calls_worker_with_bytes(self):
        import base64

        body = b'{"type":"Create"}'
        body_b64 = base64.b64encode(body).decode("ascii")
        with patch.object(
            federation_inbox, "process_inbound_activity"
        ) as mock_proc:
            federation_inbox.run_inbound_activity_task(
                actor_kind="person",
                identifier="dough",
                method="POST",
                path="/actors/dough/inbox",
                headers={"Signature": "x"},
                body_b64=body_b64,
            )
        mock_proc.assert_called_once()
        _, kwargs = mock_proc.call_args
        # The worker receives the EXACT original bytes (its signature verify
        # depends on byte-identity).
        self.assertEqual(kwargs["body"], body)
        self.assertIsInstance(kwargs["body"], bytes)

    def test_wrapper_handles_empty_body(self):
        with patch.object(
            federation_inbox, "process_inbound_activity"
        ) as mock_proc:
            federation_inbox.run_inbound_activity_task(
                actor_kind="person",
                identifier="dough",
                method="POST",
                path="/actors/dough/inbox",
                headers={},
                body_b64="",
            )
        _, kwargs = mock_proc.call_args
        self.assertEqual(kwargs["body"], b"")
