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


def resolve_model(env_var, default):
    """Resolve a per-role model id for the RAW OpenAI SDK.

    Follows the same precedence the pydantic-ai roles use (see
    api/views/admin.py _agent_role_specs): the role's own env var, then
    CADDY_DEFAULT_MODEL, then the caller's built-in default.

    The convention writes provider-prefixed ids ("openai:gpt-5"), but the
    services that call this hand the value straight to
    client.chat.completions.create(model=...), which needs a BARE id. Strip
    the prefix so one env var works for both styles of consumer.

    A non-openai provider prefix is honored as a bare name rather than
    rejected — get_client() is OpenAI-only today, so pointing these roles at
    Anthropic needs routing work, not just a config value.
    """
    raw = (
        os.environ.get(env_var)
        or os.environ.get("CADDY_DEFAULT_MODEL")
        or default
    )
    raw = str(raw).strip()
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip()
    return raw or default


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
