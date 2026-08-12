"""Model resolution + the gpt-5 temperature restriction.

Everything here was verified against the LIVE OpenAI API on 2026-08-12 while
moving answers/cover letters off gpt-4o-mini, then written down as tests so
the next model bump doesn't have to rediscover it:

  POST /chat/completions {model: gpt-5, temperature: 0.7}
  -> 400 "Unsupported value: 'temperature' does not support 0.7 with this
          model. Only the default (1) value is supported."
  the same request without `temperature` -> 200

Two behaviours protect that: retry-without-temperature (so a model bump can't
hard-fail every generation) and remember-the-rejection (so the wasted 400
happens once per process, not on every answer).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from job_hunting.lib import ai_client
from job_hunting.lib.ai_client import (
    is_temperature_error,
    resolve_model,
)
from job_hunting.lib.services.answer_service import AnswerService
from job_hunting.lib.services.cover_letter_service import CoverLetterService


# The verbatim message the API returns. If OpenAI ever rewords it, the
# detection below is what breaks — which is exactly what this asserts.
REAL_TEMPERATURE_ERROR = (
    "Error code: 400 - {'error': {'message': \"Unsupported value: "
    "'temperature' does not support 0.7 with this model. Only the default "
    "(1) value is supported.\", 'type': 'invalid_request_error', 'param': "
    "'temperature', 'code': 'unsupported_value'}}"
)


def _completion(text="generated."):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class TestResolveModel(TestCase):
    """<ROLE>_MODEL -> CADDY_DEFAULT_MODEL -> built-in default, and the
    provider prefix has to survive that trip or get rejected."""

    def test_openai_prefix_is_stripped_for_the_raw_sdk(self):
        with patch.dict("os.environ", {}, clear=False):
            ai_client.os.environ.pop("ANSWER_MODEL", None)
            ai_client.os.environ.pop("CADDY_DEFAULT_MODEL", None)
            self.assertEqual(
                resolve_model("ANSWER_MODEL", "openai:gpt-5"), "gpt-5"
            )

    def test_bare_name_passes_through(self):
        with patch.dict("os.environ", {"ANSWER_MODEL": "gpt-4o"}):
            self.assertEqual(
                resolve_model("ANSWER_MODEL", "openai:gpt-5"), "gpt-4o"
            )

    def test_role_env_beats_global_default(self):
        env = {"ANSWER_MODEL": "openai:gpt-4o", "CADDY_DEFAULT_MODEL": "openai:gpt-4o-mini"}
        with patch.dict("os.environ", env):
            self.assertEqual(
                resolve_model("ANSWER_MODEL", "openai:gpt-5"), "gpt-4o"
            )

    def test_global_default_used_when_role_env_absent(self):
        with patch.dict("os.environ", {"CADDY_DEFAULT_MODEL": "openai:gpt-4o-mini"}):
            ai_client.os.environ.pop("ANSWER_MODEL", None)
            self.assertEqual(
                resolve_model("ANSWER_MODEL", "openai:gpt-5"), "gpt-4o-mini"
            )

    def test_non_openai_provider_raises_instead_of_misrouting(self):
        """The whole point: get_client() builds an OpenAI client and nothing
        else, so stripping 'anthropic:' would hand 'claude-sonnet-4-6' to the
        wrong provider and surface as a baffling model_not_found. Same guard,
        same reason, as job_post_extractor._build_agent_for_model.
        """
        with patch.dict("os.environ", {"ANSWER_MODEL": "anthropic:claude-sonnet-4-6"}):
            with self.assertRaises(ValueError) as ctx:
                resolve_model("ANSWER_MODEL", "openai:gpt-5")
        msg = str(ctx.exception)
        # Actionable: names the offending var and the provider it rejected.
        self.assertIn("ANSWER_MODEL", msg)
        self.assertIn("anthropic", msg)

    def test_cover_letter_role_resolves_independently(self):
        env = {"COVER_LETTER_MODEL": "openai:gpt-4o", "ANSWER_MODEL": "openai:gpt-5"}
        with patch.dict("os.environ", env):
            self.assertEqual(
                resolve_model("COVER_LETTER_MODEL", "openai:gpt-5"), "gpt-4o"
            )


class TestTemperatureErrorDetection(TestCase):
    def test_detects_the_real_api_message(self):
        self.assertTrue(is_temperature_error(Exception(REAL_TEMPERATURE_ERROR)))

    def test_ignores_unrelated_errors(self):
        self.assertFalse(is_temperature_error(Exception("rate limit exceeded")))
        self.assertFalse(is_temperature_error(Exception("model_not_found")))


class TestAnswerServiceTemperatureRetry(TestCase):
    def setUp(self):
        # Module-level cache — must not leak between tests.
        ai_client._NO_TEMPERATURE_MODELS.clear()
        self.addCleanup(ai_client._NO_TEMPERATURE_MODELS.clear)
        self.client = MagicMock()

    def _svc(self):
        return AnswerService(self.client, model="gpt-5")

    def test_retries_without_temperature_and_returns_content(self):
        self.client.chat.completions.create.side_effect = [
            Exception(REAL_TEMPERATURE_ERROR),
            _completion("the answer."),
        ]
        result = self._svc()._call_ai("prompt")

        self.assertEqual(result, "the answer.")
        calls = self.client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("temperature", calls[0].kwargs)
        self.assertNotIn("temperature", calls[1].kwargs)

    def test_rejection_is_remembered_so_the_next_call_skips_temperature(self):
        """Without this, every single answer pays a wasted 400 forever."""
        self.client.chat.completions.create.side_effect = [
            Exception(REAL_TEMPERATURE_ERROR),
            _completion("first."),
            _completion("second."),
        ]
        svc = self._svc()
        svc._call_ai("prompt one")
        self.client.chat.completions.create.reset_mock()
        self.client.chat.completions.create.side_effect = [_completion("second.")]

        result = svc._call_ai("prompt two")

        self.assertEqual(result, "second.")
        calls = self.client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 1, "second call should not retry")
        self.assertNotIn("temperature", calls[0].kwargs)

    def test_a_model_without_the_restriction_keeps_its_temperature(self):
        self.client.chat.completions.create.side_effect = [_completion("ok.")]
        AnswerService(self.client, model="gpt-4o")._call_ai("prompt")
        kwargs = self.client.chat.completions.create.call_args_list[0].kwargs
        self.assertIn("temperature", kwargs)

    def test_unrelated_errors_still_propagate(self):
        """The retry must not become a catch-all that hides real failures."""
        self.client.chat.completions.create.side_effect = Exception(
            "rate limit exceeded"
        )
        with self.assertRaises(Exception) as ctx:
            self._svc()._call_ai("prompt")
        self.assertIn("rate limit", str(ctx.exception))
        self.assertEqual(self.client.chat.completions.create.call_count, 1)


class TestCoverLetterServiceTemperatureRetry(TestCase):
    def setUp(self):
        ai_client._NO_TEMPERATURE_MODELS.clear()
        self.addCleanup(ai_client._NO_TEMPERATURE_MODELS.clear)
        self.client = MagicMock()
        self.job_post = SimpleNamespace(
            id="jp1", title="Engineer", description="Build things.", company=None
        )

    def test_retries_without_temperature(self):
        self.client.chat.completions.create.side_effect = [
            Exception(REAL_TEMPERATURE_ERROR),
            _completion("Dear hiring manager.\n"),
        ]
        svc = CoverLetterService(
            self.client,
            self.job_post,
            resume_markdown="# resume",
            user_id=None,
            model="gpt-5",
        )
        result = svc.generate_cover_letter()

        self.assertIn("Dear hiring manager", result)
        calls = self.client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("temperature", calls[0].kwargs)
        self.assertNotIn("temperature", calls[1].kwargs)
