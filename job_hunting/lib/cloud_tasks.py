"""Google Cloud Tasks producer — CC-169 async dispatch on GCP Cloud Run.

The django-q2 ``qcluster`` worker is a portless process and cannot run as a
Cloud Run *service*. For the latency-sensitive, IO-bound, no-browser push
paths (cover-letter first) we enqueue a Cloud Tasks HTTP task that POSTs the
job payload to an authenticated ``/tasks/*`` handler on a separate,
IAM-private Cloud Run service running the SAME api image ("one image, two
roles"). The handler runs the identical business logic synchronously and
writes the identical durable result row.

Everything here is gated on ``settings.CC_TASKS_ENABLED``. When it is
False/unset (local dev, docker compose, any non-GCP deploy) the public
``enqueue_cover_letter`` helper falls straight back to the existing
django-q2 ``async_task`` call so those environments are completely
unaffected. Only when CC_TASKS_ENABLED is on do we import + touch
``google-cloud-tasks``.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Dotted path of the django-q2 worker function that owns the cover-letter
# business logic. The Cloud Tasks handler runs the SAME function; the
# django-q2 fallback enqueues it directly. Single source so the two
# transports never drift.
COVER_LETTER_TASK = "job_hunting.lib.tasks.cover_letter_job"

# Handler path on the tasks Cloud Run service. MUST match the terraform
# (CC-169) and the urls.py route exactly — do not rename.
COVER_LETTER_HANDLER_PATH = "/tasks/cover-letter/"

# CC-214 — the generic handler path. The unified producer ``enqueue(kind,
# **payload)`` POSTs {kind, payload} here; the generic handler dispatches by
# kind through the shared registry (job_hunting.lib.job_kinds). MUST match
# the urls.py route + terraform exactly.
RUN_JOB_HANDLER_PATH = "/tasks/run-job/"


def cloud_tasks_enabled() -> bool:
    """True when Cloud Tasks dispatch is configured + switched on."""
    return bool(getattr(settings, "CC_TASKS_ENABLED", False))


def _queue_path(client) -> str:
    """Build the fully-qualified queue path from the CC_TASKS_* settings."""
    project = getattr(settings, "GOOGLE_CLOUD_PROJECT", "") or ""
    location = getattr(settings, "CC_TASKS_LOCATION", "") or ""
    queue_id = getattr(settings, "CC_TASKS_QUEUE_ID", "") or ""
    if not (project and location and queue_id):
        raise RuntimeError(
            "Cloud Tasks misconfigured: GOOGLE_CLOUD_PROJECT / CC_TASKS_LOCATION "
            "/ CC_TASKS_QUEUE_ID must all be set when CC_TASKS_ENABLED is true"
        )
    return client.queue_path(project, location, queue_id)


def _build_http_task(handler_path: str, payload: dict, *, schedule_time=None) -> dict:
    """Build the Cloud Tasks ``Task`` dict for an authenticated HTTP POST.

    The task POSTs raw JSON to ``${CC_TASKS_HANDLER_URL}${handler_path}`` and
    carries an OIDC token minted for ``CC_TASKS_INVOKER_SA`` so the handler's
    Cloud Run ``run.invoker`` IAM binding accepts it. The audience is the
    handler base URL (Cloud Run expects the service URL as the OIDC audience).

    ``schedule_time`` (a ``datetime``, optional) sets the Cloud Tasks
    ``schedule_time`` so the task isn't delivered until then — the GCP
    equivalent of the self-host ``Job.run_after`` gate (CC-214). Omitted
    (None) → deliver immediately, preserving the cover-letter behaviour.
    """
    base = (getattr(settings, "CC_TASKS_HANDLER_URL", "") or "").rstrip("/")
    invoker_sa = getattr(settings, "CC_TASKS_INVOKER_SA", "") or ""
    if not base:
        raise RuntimeError(
            "Cloud Tasks misconfigured: CC_TASKS_HANDLER_URL must be set when "
            "CC_TASKS_ENABLED is true"
        )
    if not invoker_sa:
        raise RuntimeError(
            "Cloud Tasks misconfigured: CC_TASKS_INVOKER_SA must be set when "
            "CC_TASKS_ENABLED is true"
        )
    url = f"{base}{handler_path}"
    task: dict = {
        "http_request": {
            "http_method": "POST",
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode("utf-8"),
            "oidc_token": {
                "service_account_email": invoker_sa,
                "audience": base,
            },
        }
    }
    if schedule_time is not None:
        task["schedule_time"] = schedule_time
    return task


def _create_task(handler_path: str, payload: dict, *, schedule_time=None):
    """Create a Cloud Tasks HTTP task on the configured queue.

    Imported lazily so ``google-cloud-tasks`` is only required in GCP
    deployments where CC_TASKS_ENABLED is on.
    """
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = _queue_path(client)
    task = _build_http_task(handler_path, payload, schedule_time=schedule_time)
    return client.create_task(request={"parent": parent, "task": task})


def enqueue_cover_letter(cover_letter_id, *, injected_prompt=None) -> None:
    """Dispatch cover-letter generation, via Cloud Tasks or django-q2.

    Contract-identical to the previous inline ``async_task(COVER_LETTER_TASK,
    cover_letter_id, injected_prompt=...)`` call: same task, same args, same
    durable ``CoverLetter`` row updated by the worker/handler.

    When CC_TASKS_ENABLED is on, builds a Cloud Tasks HTTP task whose JSON
    body is the payload the handler needs. If task creation raises, we fall
    back to django-q2 so a transient Cloud Tasks fault never drops the job.
    When CC_TASKS_ENABLED is off, goes straight to django-q2.
    """
    if cloud_tasks_enabled():
        payload = {
            "cover_letter_id": cover_letter_id,
            "injected_prompt": injected_prompt,
        }
        try:
            _create_task(COVER_LETTER_HANDLER_PATH, payload)
            return
        except Exception:
            logger.exception(
                "cloud_tasks: create_task failed for cover_letter_id=%s; "
                "falling back to django-q2 async_task",
                cover_letter_id,
            )

    from django_q.tasks import async_task

    async_task(
        COVER_LETTER_TASK,
        cover_letter_id,
        injected_prompt=injected_prompt,
    )


def enqueue(kind: str, *, run_after=None, max_attempts: int = 1, **payload) -> None:
    """Unified async producer — the single ``enqueue(kind, **payload)`` API.

    ``CC_TASKS_ENABLED`` selects the transport, one or the other per
    deployment — never both, never a permanent GCP worker (CC-214):

    - **ON (GCP)**: build a Cloud Task → generic ``/tasks/run-job/`` handler,
      body ``{"kind": kind, "payload": payload}``. Scale-to-zero; Cloud Tasks'
      own managed retry is the safety net — there is NO Postgres fallback
      drainer on GCP (writing a Job row here would strand it, since GCP runs
      no Job runner). If task creation raises, we re-raise so the caller sees
      the failure rather than silently dropping the job.
    - **OFF (self-host / local)**: write a ``Job`` row for the ``run_jobs``
      pull runner to claim + dispatch by kind.

    ``run_after`` (a datetime) delays the job: Cloud Tasks ``scheduleTime`` on
    GCP, the ``Job.run_after`` gate on self-host. ``max_attempts`` is honored
    by the self-host lease sweep (GCP retry is queue-configured).

    ``payload`` must be PKs + small scalars only (never blobs) — same
    convention as the django-q2 call sites this replaces.
    """
    # Validate the kind early so a bad call fails at the producer, not deep in
    # a handler/runner. Imported here (not module top) to avoid a Django
    # app-loading import cycle.
    from job_hunting.lib.job_kinds import KIND_REGISTRY

    if kind not in KIND_REGISTRY:
        raise ValueError(f"enqueue: unknown job kind {kind!r}")

    if cloud_tasks_enabled():
        body = {"kind": kind, "payload": payload}
        # No try/except fallback here: GCP has no Job runner, so a Job row
        # would strand. Let a create_task fault surface to the caller.
        _create_task(RUN_JOB_HANDLER_PATH, body, schedule_time=run_after)
        return

    from job_hunting.models import Job

    Job.objects.create(
        kind=kind,
        payload=payload,
        run_after=run_after,
        max_attempts=max_attempts,
    )
