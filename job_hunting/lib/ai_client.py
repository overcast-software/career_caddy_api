import os

# Module-level cache
_API_KEY = None
_CLIENT = None


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


def get_client(required=False):
    """Get a cached OpenAI client, creating one if needed."""
    global _CLIENT, _API_KEY

    # If we have a cached client and key, return it
    if _CLIENT is not None and _API_KEY is not None:
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

    # Create and cache the client
    _API_KEY = current_key
    _CLIENT = OpenAI(api_key=_API_KEY)
    return _CLIENT


def set_api_key(key):
    """Set the API key and rebuild the cached client."""
    global _API_KEY, _CLIENT

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

    # Rebuild and cache the client
    _CLIENT = OpenAI(api_key=_API_KEY)


# Initialize the API key from environment on module load (but don't create client)
_API_KEY = _normalize_key(os.environ.get("OPENAI_API_KEY")) or _normalize_key(
    os.environ.get("OPENAI_API_KEY")
)
