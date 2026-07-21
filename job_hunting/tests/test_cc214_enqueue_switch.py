"""CC-214 — the unified enqueue switch (Cloud Tasks vs Job row).

The single ``enqueue(kind, **payload)`` producer picks its transport from
``CC_TASKS_ENABLED``: ON builds a Cloud Task to ``/tasks/run-job/`` (no Job
row); OFF writes a ``Job`` row (no Cloud Task). Mirrors the CC-169 producer
tests — the create-task seam is patched so no google SDK import is needed.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from job_hunting.lib import cloud_tasks
from job_hunting.models import Job

_TASKS_SETTINGS = dict(
    CC_TASKS_ENABLED=True,
    GOOGLE_CLOUD_PROJECT="cc-proj",
    CC_TASKS_LOCATION="us-central1",
    CC_TASKS_QUEUE_ID="cc-tasks",
    CC_TASKS_HANDLER_URL="https://tasks.example.run.app",
    CC_TASKS_INVOKER_SA="cc-tasks-invoker@cc-proj.iam.gserviceaccount.com",
)


class TestEnqueueSwitch(TestCase):
    @override_settings(CC_TASKS_ENABLED=False)
    def test_disabled_writes_a_job_row_no_cloud_task(self):
        with patch("job_hunting.lib.cloud_tasks._create_task") as mock_create:
            cloud_tasks.enqueue("score", score_id="abc1234567", trigger="score")
        mock_create.assert_not_called()
        job = Job.objects.get()
        self.assertEqual(job.kind, "score")
        self.assertEqual(
            job.payload, {"score_id": "abc1234567", "trigger": "score"}
        )
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.claimed_at)

    @override_settings(**_TASKS_SETTINGS)
    def test_enabled_builds_cloud_task_no_job_row(self):
        with patch("job_hunting.lib.cloud_tasks._create_task") as mock_create:
            cloud_tasks.enqueue("score", score_id="xyz9876543", trigger="auto_score")
        # No Job row written on the GCP transport (no runner would drain it).
        self.assertEqual(Job.objects.count(), 0)
        mock_create.assert_called_once()
        args, kwargs = mock_create.call_args
        self.assertEqual(args[0], cloud_tasks.RUN_JOB_HANDLER_PATH)
        self.assertEqual(
            args[1],
            {"kind": "score", "payload": {"score_id": "xyz9876543", "trigger": "auto_score"}},
        )
        # Immediate (no run_after) → schedule_time None.
        self.assertIsNone(kwargs.get("schedule_time"))

    @override_settings(**_TASKS_SETTINGS)
    def test_enabled_builder_shapes_run_job_task(self):
        # Pure builder — no google SDK import required.
        task = cloud_tasks._build_http_task(
            cloud_tasks.RUN_JOB_HANDLER_PATH,
            {"kind": "score", "payload": {"score_id": "s1"}},
        )
        http = task["http_request"]
        self.assertEqual(http["http_method"], "POST")
        self.assertEqual(
            http["url"], "https://tasks.example.run.app/tasks/run-job/"
        )
        body = json.loads(http["body"].decode("utf-8"))
        self.assertEqual(body, {"kind": "score", "payload": {"score_id": "s1"}})

    def test_unknown_kind_rejected_at_producer(self):
        with self.assertRaises(ValueError):
            cloud_tasks.enqueue("not_a_real_kind", foo=1)

    @override_settings(CC_TASKS_ENABLED=False)
    def test_run_after_and_max_attempts_persist_on_job_row(self):
        from datetime import timedelta

        from django.utils import timezone

        soon = timezone.now() + timedelta(minutes=5)
        cloud_tasks.enqueue(
            "score", run_after=soon, max_attempts=3, score_id="s9"
        )
        job = Job.objects.get()
        self.assertEqual(job.run_after, soon)
        self.assertEqual(job.max_attempts, 3)
        # run_after/max_attempts are transport controls, not worker payload.
        self.assertEqual(job.payload, {"score_id": "s9"})
