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
    # CC-202 — summary generation (summaries.py SummaryViewSet.create).
    "summary": "job_hunting.lib.tasks.summary_job",
    # CC-203 — question/answer generation (questions.py answer-create path).
    "answer": "job_hunting.lib.tasks.answer_job",
    # CC-205 — JobApplication match (jobs.py JA match-trigger create path).
    "job_application_match": "job_hunting.lib.tasks.job_application_match_job",
}


# Worker verdicts that mean "the job did NOT produce its result row" (as
# opposed to a healthy completion). Both transports (the Cloud Tasks
# /tasks/run-job/ handler and the self-host run_jobs runner) log these at
# WARNING so a job that terminates without doing its work is VISIBLE — the
# CC-214 slice was a total blackout where exactly this class of silent
# terminal verdict returned a clean success with no app-level log line.
NON_COMPLETED_VERDICTS = frozenset(
    {"missing", "failed", "source_missing", "unknown_kind"}
)


def job_ref(payload: dict) -> str:
    """A compact 'k=v' id string for the record a job payload targets.

    The generic dispatch layer doesn't know a kind's schema, so we surface
    every ``*_id`` scalar in the payload (score_id, cover_letter_id,
    scrape_id, …). This is what makes a processing log line greppable back to
    the row a job acted on. Shared by both transports so their logs match.
    """
    if not isinstance(payload, dict):
        return "(no id in payload)"
    ids = {
        k: v
        for k, v in payload.items()
        if k.endswith("_id") and isinstance(v, (str, int))
    }
    return " ".join(f"{k}={v}" for k, v in ids.items()) or "(no id in payload)"


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
