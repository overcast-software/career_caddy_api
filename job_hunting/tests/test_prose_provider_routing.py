"""CC-236 — answers and cover letters follow their configured provider.

Before this, `AnswerService` and `CoverLetterService` spoke only the raw
OpenAI SDK, so `ANSWER_MODEL=anthropic:claude-sonnet-4-6` could not work by
configuration: `ai_client.resolve_model` raised on the prefix rather than
misroute a Claude model name to the OpenAI client.

Two properties are asserted here and they pull against each other:

1. A non-OpenAI provider now routes through pydantic-ai, with the SAME
   rendered prompt and the SAME system prompt. Only the transport changes.
2. The OpenAI path is untouched. With nothing configured, both services must
   still call `client.chat.completions.create` exactly as before — including
   the gpt-5 temperature-rejection retry, which lives only on that path.

Every test mocks the model layer. Nothing here reaches a provider.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from job_hunting.lib import ai_client
from job_hunting.lib.ai_client import (
    build_prose_agent,
    provider_credential_missing,
    resolve_model_spec,
    split_model_spec,
)
from job_hunting.lib.services.answer_service import (
    ANSWER_MODEL_DEFAULT,
    ANSWER_MODEL_ENV,
    AnswerService,
)
from job_hunting.lib.services.cover_letter_service import (
    COVER_LETTER_MODEL_DEFAULT,
    COVER_LETTER_MODEL_ENV,
    CoverLetterService,
)


def _completion(text="generated."):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class _FakeAgent:
    """Stands in for a pydantic-ai Agent; records what it was asked to run."""

    def __init__(self, output="agent output."):
        self.output = output
        self.prompts = []

    def run_sync(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self.output)


class TestSplitModelSpec(TestCase):
    def test_bare_name_means_openai(self):
        """Existing deployments set ANSWER_MODEL=gpt-4o. That must keep
        meaning OpenAI, not become an error."""
        self.assertEqual(split_model_spec("gpt-4o"), ("openai", "gpt-4o"))

    def test_prefix_is_preserved_not_stripped(self):
        self.assertEqual(
            split_model_spec("anthropic:claude-sonnet-4-6"),
            ("anthropic", "claude-sonnet-4-6"),
        )

    def test_openai_prefix_yields_bare_name(self):
        self.assertEqual(split_model_spec("openai:gpt-5"), ("openai", "gpt-5"))

    def test_ollama_is_supported(self):
        self.assertEqual(split_model_spec("ollama:qwen3-coder"), ("ollama", "qwen3-coder"))

    def test_unknown_provider_raises_at_config_time(self):
        """The guard that survives from the Tier2 misroute: a provider with
        no code path fails now, naming itself, rather than surfacing as a
        model_not_found from whichever SDK got the string."""
        with self.assertRaises(ValueError) as ctx:
            split_model_spec("gemini:gemini-2.0-flash", source="ANSWER_MODEL")
        msg = str(ctx.exception)
        self.assertIn("ANSWER_MODEL", msg)
        self.assertIn("gemini", msg)

    def test_provider_with_no_model_raises(self):
        with self.assertRaises(ValueError):
            split_model_spec("anthropic:")


class TestResolveModelSpec(TestCase):
    def test_role_env_beats_global_default_beats_builtin(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_model_spec("ANSWER_MODEL", "openai:gpt-5"), ("openai", "gpt-5")
            )
        with patch.dict(os.environ, {"CADDY_DEFAULT_MODEL": "openai:gpt-4o"}, clear=True):
            self.assertEqual(
                resolve_model_spec("ANSWER_MODEL", "openai:gpt-5"), ("openai", "gpt-4o")
            )
        env = {
            "ANSWER_MODEL": "anthropic:claude-sonnet-4-6",
            "CADDY_DEFAULT_MODEL": "openai:gpt-4o",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                resolve_model_spec("ANSWER_MODEL", "openai:gpt-5"),
                ("anthropic", "claude-sonnet-4-6"),
            )

    def test_roles_resolve_independently(self):
        env = {"ANSWER_MODEL": "anthropic:claude-sonnet-4-6"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                resolve_model_spec("COVER_LETTER_MODEL", "openai:gpt-5"),
                ("openai", "gpt-5"),
            )


class TestBuildProseAgent(TestCase):
    """Provider -> model class. Mirrors the dispatch test for
    job_post_extractor._build_agent_for_model."""

    def _dispatch(self, provider, bare):
        captured = {}

        class _FakeAnthropicModel:
            def __init__(self, name):
                captured["cls"] = "AnthropicModel"
                captured["name"] = name

        class _FakeOpenAIResponsesModel:
            def __init__(self, name):
                captured["cls"] = "OpenAIResponsesModel"
                captured["name"] = name

        class _FakeAgentCls:
            def __init__(self, model, **kwargs):
                captured["kwargs"] = kwargs

        with (
            patch("pydantic_ai.models.anthropic.AnthropicModel", _FakeAnthropicModel),
            patch(
                "pydantic_ai.models.openai.OpenAIResponsesModel",
                _FakeOpenAIResponsesModel,
            ),
            patch("pydantic_ai.Agent", _FakeAgentCls),
        ):
            build_prose_agent(
                provider, bare, system_prompt="SYS", temperature=0.7, timeout=120
            )
        return captured

    def test_anthropic_builds_an_anthropic_model_with_the_bare_name(self):
        captured = self._dispatch("anthropic", "claude-sonnet-4-6")
        self.assertEqual(captured["cls"], "AnthropicModel")
        self.assertEqual(captured["name"], "claude-sonnet-4-6")

    def test_openai_builds_an_openai_model(self):
        captured = self._dispatch("openai", "gpt-5")
        self.assertEqual(captured["cls"], "OpenAIResponsesModel")
        self.assertEqual(captured["name"], "gpt-5")

    def test_system_prompt_and_settings_reach_the_agent(self):
        captured = self._dispatch("anthropic", "claude-sonnet-4-6")
        self.assertEqual(captured["kwargs"]["system_prompt"], "SYS")
        self.assertEqual(
            captured["kwargs"]["model_settings"], {"temperature": 0.7, "timeout": 120}
        )

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            build_prose_agent("gemini", "gemini-2.0-flash", system_prompt="SYS")


class TestProviderCredentialMissing(TestCase):
    """The 'can this role run?' gate. It used to ask 'is OPENAI_API_KEY set?',
    which fails a correctly-configured Anthropic stack."""

    def setUp(self):
        ai_client._API_KEY = None
        ai_client._CLIENT = None
        self.addCleanup(setattr, ai_client, "_CLIENT", None)

    def test_openai_role_without_a_key_reports_openai(self):
        with patch.dict(os.environ, {}, clear=True):
            ai_client._API_KEY = None
            msg = provider_credential_missing("ANSWER_MODEL", "openai:gpt-5")
        self.assertIn("OPENAI_API_KEY", msg)

    def test_anthropic_role_needs_only_the_anthropic_key(self):
        env = {
            "ANSWER_MODEL": "anthropic:claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        with patch.dict(os.environ, env, clear=True):
            ai_client._API_KEY = None
            self.assertIsNone(
                provider_credential_missing("ANSWER_MODEL", "openai:gpt-5")
            )

    def test_anthropic_role_without_its_key_names_that_key(self):
        env = {"ANSWER_MODEL": "anthropic:claude-sonnet-4-6", "OPENAI_API_KEY": "sk-oai"}
        with patch.dict(os.environ, env, clear=True):
            ai_client._API_KEY = None
            msg = provider_credential_missing("ANSWER_MODEL", "openai:gpt-5")
        self.assertIn("ANTHROPIC_API_KEY", msg)

    def test_ollama_needs_no_key(self):
        with patch.dict(os.environ, {"ANSWER_MODEL": "ollama:qwen3-coder"}, clear=True):
            ai_client._API_KEY = None
            self.assertIsNone(
                provider_credential_missing("ANSWER_MODEL", "openai:gpt-5")
            )


class TestAnswerServiceRouting(TestCase):
    def setUp(self):
        ai_client._NO_TEMPERATURE_MODELS.clear()
        self.addCleanup(ai_client._NO_TEMPERATURE_MODELS.clear)
        self.client = MagicMock()

    def test_anthropic_model_runs_through_pydantic_ai_not_the_openai_client(self):
        agent = _FakeAgent("a grounded answer.")
        env = {ANSWER_MODEL_ENV: "anthropic:claude-sonnet-4-6"}
        with patch.dict(os.environ, env, clear=True):
            svc = AnswerService(self.client)
            with patch.object(
                ai_client, "build_prose_agent", return_value=agent
            ) as build:
                result = svc._call_ai("the rendered prompt")

        self.assertEqual(result, "a grounded answer.")
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(agent.prompts, ["the rendered prompt"])
        args, kwargs = build.call_args
        self.assertEqual(args, ("anthropic", "claude-sonnet-4-6"))
        # The prompt text is unchanged by CC-236 — same system prompt as the
        # OpenAI path sends as its system message.
        self.assertEqual(kwargs["system_prompt"], AnswerService._SYSTEM_PROMPT)
        self.assertEqual(kwargs["temperature"], svc.temperature)
        self.assertEqual(kwargs["timeout"], AnswerService._AI_CALL_TIMEOUT)

    def test_default_configuration_still_uses_the_raw_openai_sdk(self):
        """The regression guard: nothing about the OpenAI path may move."""
        self.client.chat.completions.create.side_effect = [_completion("openai answer.")]
        with patch.dict(os.environ, {}, clear=True):
            svc = AnswerService(self.client)
            result = svc._call_ai("prompt")

        self.assertEqual(result, "openai answer.")
        self.assertEqual(svc.provider, "openai")
        kwargs = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-5")
        self.assertEqual(
            kwargs["messages"][0],
            {"role": "system", "content": AnswerService._SYSTEM_PROMPT},
        )

    def test_explicit_prefixed_model_argument_routes_too(self):
        agent = _FakeAgent("from the argument.")
        svc = AnswerService(self.client, model="anthropic:claude-haiku-4-5")
        self.assertEqual(svc.provider, "anthropic")
        self.assertEqual(svc.model, "claude-haiku-4-5")
        with patch.object(ai_client, "build_prose_agent", return_value=agent):
            self.assertEqual(svc._call_ai("p"), "from the argument.")
        self.client.chat.completions.create.assert_not_called()

    def test_bare_model_argument_stays_on_openai(self):
        svc = AnswerService(self.client, model="gpt-4o")
        self.assertEqual((svc.provider, svc.model), ("openai", "gpt-4o"))

    def test_unsupported_provider_fails_when_the_service_is_constructed(self):
        with patch.dict(os.environ, {ANSWER_MODEL_ENV: "gemini:flash"}, clear=True):
            with self.assertRaises(ValueError):
                AnswerService(self.client)


class TestCoverLetterServiceRouting(TestCase):
    def setUp(self):
        ai_client._NO_TEMPERATURE_MODELS.clear()
        self.addCleanup(ai_client._NO_TEMPERATURE_MODELS.clear)
        self.client = MagicMock()
        self.job_post = SimpleNamespace(
            id="jp1", title="Engineer", description="Build things.", company=None
        )

    def _svc(self, **kwargs):
        return CoverLetterService(
            self.client,
            self.job_post,
            resume_markdown="# resume",
            user_id=None,
            **kwargs,
        )

    def test_anthropic_model_runs_through_pydantic_ai(self):
        agent = _FakeAgent("  Dear hiring manager.\n")
        env = {COVER_LETTER_MODEL_ENV: "anthropic:claude-sonnet-4-6"}
        with patch.dict(os.environ, env, clear=True):
            svc = self._svc()
            with patch(
                "job_hunting.lib.services.cover_letter_service.build_prose_agent",
                return_value=agent,
            ) as build:
                result = svc.generate_cover_letter()

        self.assertEqual(result, "Dear hiring manager.")
        self.client.chat.completions.create.assert_not_called()
        args, kwargs = build.call_args
        self.assertEqual(args, ("anthropic", "claude-sonnet-4-6"))
        self.assertEqual(kwargs["system_prompt"], CoverLetterService._SYSTEM_PROMPT)
        self.assertEqual(kwargs["temperature"], CoverLetterService._TEMPERATURE)
        # The rendered Jinja prompt is what gets run, unchanged.
        self.assertIn("Engineer", agent.prompts[0])

    def test_default_configuration_still_uses_the_raw_openai_sdk(self):
        self.client.chat.completions.create.side_effect = [_completion("Dear sir.\n")]
        with patch.dict(os.environ, {}, clear=True):
            svc = self._svc()
            result = svc.generate_cover_letter()

        self.assertEqual(result, "Dear sir.")
        self.assertEqual(svc.provider, "openai")
        kwargs = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-5")
        self.assertEqual(
            kwargs["messages"][0],
            {"role": "system", "content": CoverLetterService._SYSTEM_PROMPT},
        )

    def test_unsupported_provider_fails_when_the_service_is_constructed(self):
        with patch.dict(os.environ, {COVER_LETTER_MODEL_ENV: "gemini:flash"}, clear=True):
            with self.assertRaises(ValueError):
                self._svc()


class TestRegisteredDefaultsStayOpenAI(TestCase):
    """CC-236 makes another provider POSSIBLE; it does not switch the default.
    Choosing a Claude default is a separate, deliberate change."""

    def test_answer_and_cover_letter_default_to_openai(self):
        self.assertEqual(split_model_spec(ANSWER_MODEL_DEFAULT)[0], "openai")
        self.assertEqual(split_model_spec(COVER_LETTER_MODEL_DEFAULT)[0], "openai")
