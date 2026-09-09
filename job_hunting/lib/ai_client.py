import os

# Module-level cache
_API_KEY = None
_CLIENT = None
_TIMEOUT = None


def _normalize_key(key):
    """Normalize API key by stripping whitespace and returning None if empty."""
    if key is None:
        return None
    key = str(key).strip()
    return key if key else None


def get_api_key(required=False):
    """Get the currently effective API key from cache or environment."""
    global _API_KEY

    if _API_KEY is None:
        _API_KEY = _normalize_key(os.environ.get("OPENAI_API_KEY")) or _normalize_key(
            os.environ.get("OPENAI_API_KEY")
        )

    if _API_KEY is None and required:
        raise RuntimeError("OPENAI_API_KEY not configured")

    return _API_KEY


# The providers this api can actually build a client for. Mirrors the
# dispatch in job_post_extractor._build_agent_for_model, job_matcher,
# description_arbiter and completeness_reviewer — a provider absent here has
# no code path and must fail at config time, not on the first generation.
SUPPORTED_PROVIDERS = ("openai", "anthropic", "ollama")

# Env var holding each provider's credential. `ollama` is local and needs
# none, so it has no entry.
_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _raw_model_setting(env_var, default):
    """The role's configured model string, before any parsing.

    Precedence is the one api/views/admin.py _agent_role_specs documents:
    the role's own env var, then CADDY_DEFAULT_MODEL, then the caller's
    built-in default.
    """
    raw = os.environ.get(env_var) or os.environ.get("CADDY_DEFAULT_MODEL") or default
    return str(raw).strip() or str(default).strip()


def split_model_spec(spec, source="model"):
    """Split a model setting into (provider, bare_name).

    A value with no prefix is read as OpenAI — that is what every bare id in
    this codebase has always meant, and existing deployments set
    ANSWER_MODEL=gpt-4o.

    An UNKNOWN provider raises here, at config time, rather than surfacing
    later as a baffling model_not_found from whichever SDK happened to get
    the string. That is the same guard, for the same reason, as
    job_post_extractor._build_agent_for_model — added after the Tier2
    misroute (scrape #237 / jp 1550, 2026-04-30).
    """
    spec = str(spec).strip()
    if ":" not in spec:
        return "openai", spec
    provider, bare = spec.split(":", 1)
    provider, bare = provider.strip().lower(), bare.strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"{source}={spec!r} names provider {provider!r}, which this api "
            f"cannot build a client for. Supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)}."
        )
    if not bare:
        raise ValueError(f"{source}={spec!r} names provider {provider!r} with no model.")
    return provider, bare


def resolve_model_spec(env_var, default):
    """Resolve a per-role model to (provider, bare_name), prefix PRESERVED.

    This is the entry point for a role that can run on more than one
    provider. `resolve_model` below is the narrower OpenAI-only sibling, kept
    for callers that hand the id straight to the raw OpenAI SDK.
    """
    return split_model_spec(_raw_model_setting(env_var, default), source=env_var)


def resolve_model(env_var, default):
    """Resolve a per-role model id for the RAW OpenAI SDK.

    Same precedence as resolve_model_spec, but returns a BARE id — the shape
    client.chat.completions.create(model=...) needs — and RAISES on any
    non-openai provider, because get_client() builds an OpenAI client and
    nothing else.

    Use this only where the call really is the raw OpenAI SDK. A role that
    should follow its configured provider wants resolve_model_spec +
    build_prose_agent instead (see AnswerService / CoverLetterService).
    """
    provider, bare = resolve_model_spec(env_var, default)
    if provider != "openai":
        raise ValueError(
            f"{env_var} names provider {provider!r}, but this call site "
            "runs on the OpenAI client only (job_hunting.lib.ai_client."
            "get_client). Use an 'openai:' model here, or move the call site "
            "onto the pydantic-ai path (ai_client.build_prose_agent)."
        )
    return bare


def build_prose_agent(provider, bare_name, system_prompt, temperature=None, timeout=None):
    """A pydantic-ai Agent for a plain-prose role, bound to `provider`.

    Prose roles have no structured output_type — the model's job is to write
    text — so this returns a str-output Agent. Everything else mirrors
    job_post_extractor._build_agent_for_model: one model class per provider,
    the anthropic SDK imported lazily so installs that only configure
    OpenAI/Ollama don't need it.

    `temperature` and `timeout` become model_settings. On the OpenAI side
    both prose services still use the raw SDK, so the gpt-5
    temperature-rejection retry (see rejects_temperature below) stays where
    it is and does not need repeating here.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from pydantic_ai.providers.ollama import OllamaProvider

    if provider == "ollama":
        model = OpenAIChatModel(
            model_name=bare_name,
            provider=OllamaProvider(
                base_url=os.environ.get(
                    "OLLAMA_API_BASE", "http://localhost:11434/v1"
                )
            ),
        )
    elif provider == "anthropic":
        # Lazy — keeps the anthropic SDK optional.
        from pydantic_ai.models.anthropic import AnthropicModel

        model = AnthropicModel(bare_name)
    elif provider == "openai":
        model = OpenAIResponsesModel(bare_name)
    else:
        raise ValueError(
            f"Unknown provider {provider!r}. Supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)}."
        )

    settings = {}
    if temperature is not None:
        settings["temperature"] = temperature
    if timeout is not None:
        settings["timeout"] = timeout

    return Agent(
        model,
        system_prompt=system_prompt,
        model_settings=settings or None,
    )


def provider_credential_missing(env_var, default):
    """Message naming the missing credential for a role's provider, or None.

    The callers that gate an AI feature on `get_client() is None` were really
    asking "can this role run?", and the answer stopped being "is
    OPENAI_API_KEY set?" once a role could be pointed at Anthropic. Returns
    the same wording those gates already used, so an OpenAI-configured
    deployment sees exactly the response it saw before.
    """
    provider, _ = resolve_model_spec(env_var, default)
    if provider == "openai":
        if get_client(required=False) is None:
            return "AI client not configured. Set OPENAI_API_KEY."
        return None
    key_env = _PROVIDER_KEY_ENV.get(provider)
    if key_env and not _normalize_key(os.environ.get(key_env)):
        return f"AI client not configured. Set {key_env}."
    return None


# Models observed at RUNTIME to reject an explicit `temperature`. Learned from
# the API's own 400 rather than hardcoded, so it can't rot when OpenAI ships
# the next model — but cached, so the wasted round-trip happens at most once
# per model per process instead of on every single generation.
#
# VERIFIED 2026-08-12 against the live API: gpt-5 returns
#   400 "Unsupported value: 'temperature' does not support 0.7 with this
#        model. Only the default (1) value is supported."
# and the same request without `temperature` returns 200.
_NO_TEMPERATURE_MODELS = set()


def rejects_temperature(model):
    """True if this model has already 400'd on an explicit temperature."""
    return model in _NO_TEMPERATURE_MODELS


def note_temperature_rejected(model):
    """Record that `model` rejects an explicit temperature."""
    _NO_TEMPERATURE_MODELS.add(model)


def is_temperature_error(exc):
    """Whether an exception is the API complaining about `temperature`.

    Matched on the error text, not a model allowlist — the set of models with
    this restriction changes with every release, but the message does not.
    """
    return "temperature" in str(exc).lower()


def _read_timeout_env():
    """Read OpenAI HTTP timeout (in seconds) from environment without caching."""
    val = os.environ.get("OPENAI_TIMEOUT_SECONDS") or os.environ.get("OPENAI_TIMEOUT_SECS") or os.environ.get("OPENAI_HTTP_TIMEOUT")
    try:
        t = float(val) if val is not None else 900.0
    except Exception:
        t = 900.0
    if t and t > 0:
        return t
    return 900.0


def get_client(required=False):
    """Get a cached OpenAI client, creating one if needed."""
    global _CLIENT, _API_KEY, _TIMEOUT

    current_timeout = _read_timeout_env()

    # If we have a cached client and key and timeout hasn't changed, return it
    if _CLIENT is not None and _API_KEY is not None and _TIMEOUT == current_timeout:
        return _CLIENT

    # Try to get/refresh the API key
    current_key = get_api_key(required=False)
    if current_key is None:
        if required:
            raise RuntimeError("OPENAI_API_KEY not configured")
        return None

    # Import OpenAI only when we need to create a client
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "OpenAI package is required but not installed. Install with: pip install openai"
        )

    # Create and cache the client with configured timeout
    _API_KEY = current_key
    _TIMEOUT = current_timeout
    _CLIENT = OpenAI(api_key=_API_KEY, timeout=_TIMEOUT)
    return _CLIENT


def set_api_key(key):
    """Set the API key and rebuild the cached client."""
    global _API_KEY, _CLIENT, _TIMEOUT

    normalized_key = _normalize_key(key)
    if normalized_key is None:
        raise ValueError("OPENAI_API_KEY must be a non-empty string")

    # Update environment and cache
    os.environ["OPENAI_API_KEY"] = normalized_key
    _API_KEY = normalized_key

    # Import OpenAI only when we need to create a client
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "OpenAI package is required but not installed. Install with: pip install openai"
        )

    # Rebuild and cache the client with configured timeout
    current_timeout = _read_timeout_env()
    _TIMEOUT = current_timeout
    _CLIENT = OpenAI(api_key=_API_KEY, timeout=_TIMEOUT)


# Initialize the API key from environment on module load (but don't create client)
_API_KEY = _normalize_key(os.environ.get("OPENAI_API_KEY")) or _normalize_key(
    os.environ.get("OPENAI_API_KEY")
)
# Initialize timeout from environment (seconds)
_TIMEOUT = (_read_timeout_env())
