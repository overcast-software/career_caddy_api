"""CC-169 — cover-letter dispatch via Cloud Tasks (producer + handler).

Covers:
  - producer builds the correct Cloud Tasks HTTP task (client mocked)
  - producer falls back to django-q2 async_task when CC_TASKS_ENABLED off
  - producer falls back to django-q2 if create_task raises
  - handler runs the SAME worker + writes the SAME row, returns 200
  - handler is idempotent on Cloud Tasks retry (row re-updated in place)
  - handler rejects malformed payloads and honours the terminal
    ``missing``/``failed`` worker verdicts as 200 (no retry storm)
"""

import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from job_hunting.lib import cloud_tasks
from job_hunting.models import Company, CoverLetter, JobPost, Resume

User = get_user_model()

_TASKS_SETTINGS = dict(
    CC_TASKS_ENABLED=True,
    GOOGLE_CLOUD_PROJECT="cc-proj",
    CC_TASKS_LOCATION="us-central1",
    CC_TASKS_QUEUE_ID="cc-tasks",
    CC_TASKS_HANDLER_URL="https://tasks.example.run.app",
    CC_TASKS_INVOKER_SA="cc-tasks-invoker@cc-proj.iam.gserviceaccount.com",
)


class TestEnqueueCoverLetterProducer(TestCase):
    @override_settings(CC_TASKS_ENABLED=False)
    def test_falls_back_to_django_q_when_disabled(self):
        with patch("django_q.tasks.async_task") as mock_async, patch(
            "job_hunting.lib.cloud_tasks._create_task"
        ) as mock_create:
            cloud_tasks.enqueue_cover_letter("abc1234567", injected_prompt="hi")
        mock_create.assert_not_called()
        mock_async.assert_called_once_with(
            cloud_tasks.COVER_LETTER_TASK,
            "abc1234567",
            injected_prompt="hi",
        )

    @override_settings(**_TASKS_SETTINGS)
    def test_builds_correct_http_task(self):
        # Pure builder — no Google SDK import required.
        task = cloud_tasks._build_http_task(
            cloud_tasks.COVER_LETTER_HANDLER_PATH,
            {"cover_letter_id": "xyz9876543", "injected_prompt": "tone: warm"},
        )
        http = task["http_request"]
        self.assertEqual(http["http_method"], "POST")
        self.assertEqual(
            http["url"], "https://tasks.example.run.app/tasks/cover-letter/"
        )
        self.assertEqual(http["headers"]["Content-Type"], "application/json")
        body = json.loads(http["body"].decode("utf-8"))
        self.assertEqual(
            body,
            {"cover_letter_id": "xyz9876543", "injected_prompt": "tone: warm"},
        )
        oidc = http["oidc_token"]
        self.assertEqual(
            oidc["service_account_email"],
            "cc-tasks-invoker@cc-proj.iam.gserviceaccount.com",
        )
        self.assertEqual(oidc["audience"], "https://tasks.example.run.app")

    @override_settings(**_TASKS_SETTINGS)
    def test_queue_path_from_settings(self):
        client = MagicMock()
        client.queue_path.return_value = "QPATH"
        self.assertEqual(cloud_tasks._queue_path(client), "QPATH")
        client.queue_path.assert_called_once_with(
            "cc-proj", "us-central1", "cc-tasks"
        )

    @override_settings(**_TASKS_SETTINGS)
    def test_enabled_dispatches_via_cloud_tasks_not_django_q(self):
        with patch(
            "job_hunting.lib.cloud_tasks._create_task"
        ) as mock_create, patch("django_q.tasks.async_task") as mock_async:
            cloud_tasks.enqueue_cover_letter("xyz9876543", injected_prompt="tone: warm")
        mock_async.assert_not_called()
        mock_create.assert_called_once_with(
            cloud_tasks.COVER_LETTER_HANDLER_PATH,
            {"cover_letter_id": "xyz9876543", "injected_prompt": "tone: warm"},
        )

    @override_settings(**_TASKS_SETTINGS)
    def test_falls_back_to_django_q_when_create_task_raises(self):
        with patch(
            "job_hunting.lib.cloud_tasks._create_task",
            side_effect=RuntimeError("boom"),
        ), patch("django_q.tasks.async_task") as mock_async:
            cloud_tasks.enqueue_cover_letter("id01234567", injected_prompt=None)
        mock_async.assert_called_once_with(
            cloud_tasks.COVER_LETTER_TASK,
            "id01234567",
            injected_prompt=None,
        )


class TestCoverLetterTaskHandler(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="cl_handler", password="pw")
        self.company = Company.objects.create(name="Acme")
        self.job_post = JobPost.objects.create(
            title="Backend Engineer",
            description="Build things.",
            company=self.company,
            created_by=self.user,
        )
        self.resume = Resume.objects.create(
            user=self.user, name="Primary", title="Backend Engineer"
        )
        self.cover_letter = CoverLetter.objects.create(
            user_id=self.user.id,
            resume_id=self.resume.id,
            job_post_id=self.job_post.id,
            company_id=self.company.id,
            status="pending",
        )
        self.url = reverse("tasks-cover-letter")

    def _post(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _patch_generation(self, content="Dear team, ... Regards."):
        """Patch the worker's AI leg so no network is touched."""
        client_patch = patch(
            "job_hunting.lib.ai_client.get_client", return_value=MagicMock()
        )
        gen_patch = patch(
            "job_hunting.lib.services.cover_letter_service.CoverLetterService.generate_cover_letter",
            return_value=content,
        )
        return client_patch, gen_patch

    def test_handler_runs_logic_and_writes_row(self):
        client_patch, gen_patch = self._patch_generation("Generated letter one.")
        with client_patch, gen_patch:
            resp = self._post({"cover_letter_id": self.cover_letter.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "completed")
        self.cover_letter.refresh_from_db()
        self.assertEqual(self.cover_letter.status, "completed")
        self.assertEqual(self.cover_letter.content, "Generated letter one.")

    def test_handler_idempotent_on_retry(self):
        # First delivery.
        client_patch, gen_patch = self._patch_generation("First body.")
        with client_patch, gen_patch:
            self._post({"cover_letter_id": self.cover_letter.id})
        # Cloud Tasks retry — same row is regenerated in place, not duplicated.
        client_patch2, gen_patch2 = self._patch_generation("Second body.")
        with client_patch2, gen_patch2:
            resp = self._post({"cover_letter_id": self.cover_letter.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            CoverLetter.objects.filter(pk=self.cover_letter.id).count(), 1
        )
        self.cover_letter.refresh_from_db()
        self.assertEqual(self.cover_letter.status, "completed")
        self.assertEqual(self.cover_letter.content, "Second body.")

    def test_handler_rejects_missing_id(self):
        resp = self._post({"injected_prompt": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_handler_rejects_invalid_json(self):
        resp = self.client.post(
            self.url, data="not json", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_handler_returns_200_for_deleted_row(self):
        # A vanished row is a terminal "missing" verdict — 200, never a retry.
        # Capture the id before delete (Django nulls the instance pk on delete).
        cl_id = self.cover_letter.id
        self.cover_letter.delete()
        resp = self._post({"cover_letter_id": cl_id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "missing")

    def test_handler_rejects_get(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 405)
