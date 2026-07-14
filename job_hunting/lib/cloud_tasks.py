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


def _build_http_task(handler_path: str, payload: dict) -> dict:
    """Build the Cloud Tasks ``Task`` dict for an authenticated HTTP POST.

    The task POSTs raw JSON to ``${CC_TASKS_HANDLER_URL}${handler_path}`` and
    carries an OIDC token minted for ``CC_TASKS_INVOKER_SA`` so the handler's
    Cloud Run ``run.invoker`` IAM binding accepts it. The audience is the
    handler base URL (Cloud Run expects the service URL as the OIDC audience).
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
    return {
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


def _create_task(handler_path: str, payload: dict):
    """Create a Cloud Tasks HTTP task on the configured queue.

    Imported lazily so ``google-cloud-tasks`` is only required in GCP
    deployments where CC_TASKS_ENABLED is on.
    """
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = _queue_path(client)
    task = _build_http_task(handler_path, payload)
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
