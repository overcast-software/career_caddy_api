"""Central logfire wiring for the Django api.

Called from ``settings.py`` after the ``LOGGING`` dict has been defined.
When ``LOGFIRE_TOKEN`` is set the helper:

  1. configures logfire with the ``career_caddy_api`` service name,
  2. attaches ``LogfireLoggingHandler`` to the stdlib root logger so
     every ``logger.info(...)`` in views, services, and ORM-adjacent
     code flows to logfire — matches the ai-side helper's contract,
  3. calls ``instrument_django()`` so every request gets a span with
     method, path, status, duration, and attached DB query count,
  4. auto-instruments openai / anthropic / httpx so the services that
     call LLMs from inside Django requests (answer generation, cover
     letters, score generation) get end-to-end traces.

When the token is unset the call is a silent no-op.
"""
from __future__ import annotations

import logging
import os

_SETUP_DONE_KEY = "_cc_logfire_setup_done"


def setup_logfire(service_name: str = "career_caddy_api") -> bool:
    """Configure logfire for the Django process. Returns True if
    logfire was actually wired, False if LOGFIRE_TOKEN was unset."""
    if not os.environ.get("LOGFIRE_TOKEN"):
        return False
    if os.environ.get(_SETUP_DONE_KEY) == "1":
        return True

    try:
        import logfire
    except ImportError:
        logging.getLogger(__name__).warning(
            "LOGFIRE_TOKEN set but logfire package not installed; skipping setup"
        )
        return False

    logfire.configure(
        service_name=service_name,
        scrubbing=False,
        console=False,
    )

    from logfire.integrations.logging import LogfireLoggingHandler

    handler = LogfireLoggingHandler()
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    already = any(isinstance(h, LogfireLoggingHandler) for h in root.handlers)
    if not already:
        root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    # Django request spans + LLM call spans. Each instrumenter is
    # best-effort; missing libraries don't block the others.
    for fn_name in (
        "instrument_django",
        "instrument_openai",
        "instrument_anthropic",
        "instrument_httpx",
    ):
        fn = getattr(logfire, fn_name, None)
        if fn is None:
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "logfire.%s failed: %s", fn_name, exc
            )

    os.environ[_SETUP_DONE_KEY] = "1"
    logging.getLogger(__name__).info(
        "logfire configured — service=%s", service_name
    )
    return True
