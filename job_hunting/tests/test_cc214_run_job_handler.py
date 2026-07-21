"""CC-214 — the generic /tasks/run-job/ handler + run_jobs dispatch.

Mirrors the CC-169 cover-letter handler tests: drive the plain view via the
Django test client and dispatch by ``kind``. The registered worker fn is
patched (via the shared registry) so these exercise the HANDLER + RUNNER
dispatch mechanics — kind resolution, payload forwarding, terminal-verdict
pass-through, unknown-kind + malformed handling — without dragging in the
score worker's own AI/CareerData preconditions. A dedicated score-worker test
covers score_job itself elsewhere.
"""

import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from job_hunting.models import Job


class TestRunJobHandler(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("tasks-run-job")

    def _post(self, body):
        return self.client.post(
            self.url, data=json.dumps(body), content_type="application/json"
        )

    def test_dispatches_score_kind_with_payload(self):
        with patch(
            "job_hunting.lib.tasks.score_job",
            return_value={"score": 91, "status": "completed"},
        ) as mock_score:
            resp = self._post(
                {"kind": "score", "payload": {"score_id": "s1", "trigger": "score"}}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "completed")
        # Payload forwarded verbatim as kwargs to the registered worker.
        mock_score.assert_called_once_with(score_id="s1", trigger="score")

    def test_terminal_missing_verdict_is_200_no_retry(self):
        # A worker's own terminal verdict must NOT trigger a Cloud Tasks retry.
        with patch(
            "job_hunting.lib.tasks.score_job",
            return_value={"score": None, "status": "missing"},
        ):
            resp = self._post({"kind": "score", "payload": {"score_id": "gone"}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "missing")

    def test_worker_exception_propagates_to_500_for_retry(self):
        # A retryable fault re-raised inside the worker → 500 → Cloud Tasks
        # retries (Django test client re-raises by default; assert that).
        with patch(
            "job_hunting.lib.tasks.score_job", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self._post({"kind": "score", "payload": {"score_id": "s1"}})

    def test_unknown_kind_is_terminal_200(self):
        resp = self._post({"kind": "does_not_exist", "payload": {}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "unknown_kind")

    def test_missing_kind_400(self):
        resp = self._post({"payload": {"score_id": "x"}})
        self.assertEqual(resp.status_code, 400)

    def test_non_object_payload_400(self):
        resp = self._post({"kind": "score", "payload": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_json_400(self):
        resp = self.client.post(
            self.url, data="not json", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_rejects_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


class TestRunJobsCommand(TestCase):
    """The self-host executor claims a Job, dispatches by kind, records status."""

    def test_run_jobs_once_drains_and_completes(self):
        Job.objects.create(kind="score", payload={"score_id": "s1", "trigger": "score"})
        with patch(
            "job_hunting.lib.tasks.score_job",
            return_value={"status": "completed"},
        ) as mock_score:
            call_command("run_jobs", "--once", "--runner-name", "test", "--sweep-every", "0")
        mock_score.assert_called_once_with(score_id="s1", trigger="score")
        self.assertEqual(Job.objects.get().status, "completed")

    def test_run_jobs_worker_exception_marks_failed(self):
        Job.objects.create(kind="score", payload={"score_id": "s1"})
        with patch(
            "job_hunting.lib.tasks.score_job", side_effect=RuntimeError("boom")
        ):
            call_command("run_jobs", "--once", "--runner-name", "test", "--sweep-every", "0")
        self.assertEqual(Job.objects.get().status, "failed")

    def test_run_jobs_unknown_kind_marks_failed(self):
        Job.objects.create(kind="nope", payload={})
        call_command("run_jobs", "--once", "--runner-name", "test", "--sweep-every", "0")
        self.assertEqual(Job.objects.get().status, "failed")

    def test_run_jobs_empty_queue_exits_clean(self):
        # No rows → --once returns immediately without error.
        call_command("run_jobs", "--once", "--runner-name", "test", "--sweep-every", "0")
        self.assertEqual(Job.objects.count(), 0)
