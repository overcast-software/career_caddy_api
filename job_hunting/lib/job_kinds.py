"""Kind registry — the single source mapping a Job ``kind`` to its worker fn.

Both async transports consult this ONE dict so they can never drift:

- the Cloud Tasks handler (``/tasks/run-job/``, GCP) dispatches by ``kind``
- the self-host runner (``manage.py run_jobs``) dispatches by ``kind``

Mirrors the ``COVER_LETTER_TASK`` const pattern from CC-169 (a single dotted
path both transports shared), generalized to N kinds. Worker fns live in
``job_hunting.lib.tasks`` and already take PKs + re-fetch, so they are
transport-agnostic and safe to run synchronously from either side.

To migrate a path: add its ``kind`` here, then flip the call site from
``async_task("...", ...)`` to ``enqueue("<kind>", **payload)``. The registry
is the only place the kind→fn mapping is written.
"""

from __future__ import annotations

from importlib import import_module

# kind -> dotted path of the lib/tasks.py worker fn. Import is lazy (resolved
# in resolve_kind) so importing this module stays cheap and doesn't drag in
# the AI/scoring dependency graph at Django startup.
KIND_REGISTRY: dict[str, str] = {
    # CC-214 validation slice: the first migrated path.
    "score": "job_hunting.lib.tasks.score_job",
    # CC-206 — ActivityPub outbound dispatch (fire-now + run_after delay). The
    # retry state machine (FederationActivity.retry_count/next_attempt_at +
    # sweep_pending_dispatches, migrated to the GCP clock by CC-213) is
    # unchanged; this is only the enqueue seam.
    "federation_dispatch": "job_hunting.lib.federation_dispatch.dispatch_one",
    # CC-206 — ActivityPub inbound verify+process. The raw request body rides
    # the JSON payload base64-encoded (AP bodies are small); the task wrapper
    # base64-decodes before calling process_inbound_activity.
    "federation_inbox": "job_hunting.lib.federation_inbox.run_inbound_activity_task",
}


class UnknownKind(KeyError):
    """Raised when a Job/handler references a kind not in the registry."""


def resolve_kind(kind: str):
    """Return the callable worker fn registered for ``kind``.

    Raises ``UnknownKind`` if the kind isn't registered — the caller decides
    whether that's a terminal 200 (handler: a bad task never becomes good) or
    a failed Job (runner). Never dispatches an arbitrary importable: only the
    fixed set of dotted paths in ``KIND_REGISTRY`` is reachable.
    """
    dotted = KIND_REGISTRY.get(kind)
    if dotted is None:
        raise UnknownKind(kind)
    module_path, _, attr = dotted.rpartition(".")
    module = import_module(module_path)
    return getattr(module, attr)
