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
import base64
from unittest.mock import patch

from django.conf import settings
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


@override_settings(ACTIVITYPUB_INBOX_DISPATCH_SYNC=False)
class TestInboxOversizedBodyStaysInBand(TestCase):
    """CC-220 — bodies too big for a Cloud Task are processed in-band.

    ``enqueue_inbound_activity`` carries the body inline as base64 (CC-206),
    which inflates it ~33%. Cloud Tasks caps a task at ~1 MB while the edge
    accepts up to ``ACTIVITYPUB_BODY_MAX_BYTES`` (~1 MB) — so a near-maximal
    activity would produce a ~1.33 MB task and fail to enqueue on GCP.
    Anything above ``ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES`` therefore skips the
    queue and runs through ``process_inbound_activity`` synchronously.
    """

    @override_settings(ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES=64)
    def test_oversized_body_does_not_enqueue_and_runs_in_band(self):
        body = b"x" * 65
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            with patch.object(
                federation_inbox, "process_inbound_activity"
            ) as mock_proc:
                federation_inbox.enqueue_inbound_activity(
                    actor_kind="person",
                    identifier="dough",
                    method="POST",
                    path="/actors/dough/inbox",
                    headers={"Signature": "keyId=..."},
                    body=body,
                )
        mock_enqueue.assert_not_called()
        mock_proc.assert_called_once()
        _, kwargs = mock_proc.call_args
        # The in-band worker still gets the EXACT bytes it verifies against.
        self.assertEqual(kwargs["body"], body)
        self.assertIsInstance(kwargs["body"], bytes)
        self.assertEqual(kwargs["headers"]["Signature"], "keyId=...")

    @override_settings(ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES=64)
    def test_body_at_the_cap_still_enqueues(self):
        # The cap is inclusive — only bodies STRICTLY larger fall back.
        body = b"x" * 64
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            with patch.object(
                federation_inbox, "process_inbound_activity"
            ) as mock_proc:
                federation_inbox.enqueue_inbound_activity(
                    actor_kind="person",
                    identifier="dough",
                    method="POST",
                    path="/actors/dough/inbox",
                    headers={},
                    body=body,
                )
        mock_proc.assert_not_called()
        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "federation_inbox")
        self.assertEqual(base64.b64decode(kwargs["body_b64"]), body)

    @override_settings(ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES=64)
    def test_small_body_still_enqueues(self):
        body = b'{"type":"Follow","actor":"https://peer.example/u/x"}'
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            with patch.object(
                federation_inbox, "process_inbound_activity"
            ) as mock_proc:
                federation_inbox.enqueue_inbound_activity(
                    actor_kind="company",
                    identifier="acme",
                    method="POST",
                    path="/companies/acme/inbox",
                    headers={},
                    body=body,
                )
        mock_proc.assert_not_called()
        mock_enqueue.assert_called_once()
        _, kwargs = mock_enqueue.call_args
        self.assertEqual(base64.b64decode(kwargs["body_b64"]), body)

    @override_settings(ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES=8)
    def test_oversized_str_body_is_encoded_before_the_in_band_call(self):
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            with patch.object(
                federation_inbox, "process_inbound_activity"
            ) as mock_proc:
                federation_inbox.enqueue_inbound_activity(
                    actor_kind="person",
                    identifier="dough",
                    method="POST",
                    path="/actors/dough/inbox",
                    headers={},
                    body='{"type":"Delete"}',
                )
        mock_enqueue.assert_not_called()
        _, kwargs = mock_proc.call_args
        self.assertEqual(kwargs["body"], b'{"type":"Delete"}')

    def test_default_async_cap_keeps_a_max_edge_body_out_of_the_queue(self):
        # The real defaults must line up: a body at the edge's accept limit
        # (ACTIVITYPUB_BODY_MAX_BYTES) base64s to ~1.33 MB, over the Cloud
        # Tasks ~1 MB task cap — so the default async cap has to be lower.
        self.assertLess(
            settings.ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES,
            settings.ACTIVITYPUB_BODY_MAX_BYTES,
        )
        # And base64 of an at-cap body must still fit under 1 MB.
        self.assertLess(
            (settings.ACTIVITYPUB_INBOX_ASYNC_MAX_BYTES + 2) // 3 * 4,
            1_000_000,
        )
        body = b"y" * settings.ACTIVITYPUB_BODY_MAX_BYTES
        with patch("job_hunting.lib.cloud_tasks.enqueue") as mock_enqueue:
            with patch.object(
                federation_inbox, "process_inbound_activity"
            ) as mock_proc:
                federation_inbox.enqueue_inbound_activity(
                    actor_kind="person",
                    identifier="dough",
                    method="POST",
                    path="/actors/dough/inbox",
                    headers={},
                    body=body,
                )
        mock_enqueue.assert_not_called()
        mock_proc.assert_called_once()
