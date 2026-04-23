"""Regression test: resume_importer now records AiUsage rows.

Before the fix, IngestResume.process() called agent.run_sync() and never
emitted an AiUsage row — the import showed $0 in the costs dashboard even
though it's one of the more expensive LLM calls in the app.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase

from job_hunting.lib.services.ingest_resume import IngestResume
from job_hunting.models import AiUsage


User = get_user_model()


class StubUsage(SimpleNamespace):
    """Shape-compatible with pydantic_ai.usage.RequestUsage."""


class TestResumeImporterUsageRecording(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="doug", password="p")

    def _run_record_usage_with(self, result_usage):
        ingest = IngestResume(user=self.user)
        # Seed .agent so _get_model_name doesn't return "unknown" — exercise
        # the OpenAIChatModel (ollama-labelled) branch.
        ingest.agent = MagicMock()
        ingest.agent.model = MagicMock()
        # type(model).__name__ drives the label; use a class we label as openai.
        ingest.agent.model.__class__.__name__ = "OpenAIModel"
        ingest.agent.model.model_name = "gpt-4o-mini"

        result = MagicMock()
        result.usage.return_value = result_usage
        ingest._record_usage(result)

    def test_records_row_with_real_tokens(self):
        self._run_record_usage_with(
            StubUsage(
                request_tokens=1200,
                response_tokens=800,
                total_tokens=2000,
                requests=1,
            )
        )
        row = AiUsage.objects.get()
        self.assertEqual(row.agent_name, "resume_importer")
        self.assertEqual(row.trigger, "resume_import")
        self.assertEqual(row.model_name, "openai:gpt-4o-mini")
        self.assertEqual(row.request_tokens, 1200)
        self.assertEqual(row.response_tokens, 800)
        self.assertEqual(row.total_tokens, 2000)
        self.assertEqual(row.user, self.user)
        self.assertGreater(row.estimated_cost_usd, Decimal("0"))

    def test_missing_usage_fields_defaults_to_zero(self):
        """Legacy test stubs return usage() as a dict — must not crash or
        produce negative counts, just record zeros."""
        self._run_record_usage_with({})  # dict — no .request_tokens attr
        row = AiUsage.objects.get()
        self.assertEqual(row.request_tokens, 0)
        self.assertEqual(row.response_tokens, 0)
        self.assertEqual(row.total_tokens, 0)
        self.assertEqual(row.estimated_cost_usd, Decimal("0"))

    def test_no_user_falls_back_to_first_staff(self):
        """Programmatic ingest paths (e.g. bulk imports) may omit user; the
        row still needs an owner for cost attribution."""
        staff = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )

        ingest = IngestResume()  # no user passed
        ingest.agent = MagicMock()
        ingest.agent.model = MagicMock()
        ingest.agent.model.__class__.__name__ = "OpenAIModel"
        ingest.agent.model.model_name = "gpt-4o-mini"

        result = MagicMock()
        result.usage.return_value = StubUsage(
            request_tokens=1, response_tokens=1, total_tokens=2, requests=1,
        )
        ingest._record_usage(result)

        row = AiUsage.objects.get()
        self.assertEqual(row.user, staff)
