"""Cloud Tasks HTTP handlers — CC-169 async dispatch on GCP Cloud Run.

These are plain Django views (NOT DRF / JSON:API) because Cloud Tasks POSTs
raw JSON, not a JSON:API document. Each handler parses the task payload and
runs the SAME business logic synchronously as the corresponding django-q2
worker function, writing the SAME durable result row.

Safety-to-re-run is load-bearing: any non-2xx response makes Cloud Tasks
retry, so a handler must be safe to run twice. The cover-letter path is:
the worker re-fetches the ``CoverLetter`` row by pk and ``.update()``s it,
so a retry just regenerates + overwrites the same row — idempotent by row
identity.

Auth: the tasks service is IAM-private at the infra layer — Cloud Run only
admits requests bearing a valid OIDC token for the invoker SA, so the
handler never runs for an un-authenticated caller. As defense-in-depth we
additionally require the ``X-CloudTasks-*`` headers Cloud Tasks always
stamps on delivered tasks (unforgeable by external callers, which Cloud Run
strips). The guard is skipped under TESTING and when explicitly disabled.
"""

from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

# Header Cloud Tasks stamps on every delivered task (the queue name). Cloud
# Run strips client-supplied X-CloudTasks-* headers, so its presence is a
# reliable "this came from Cloud Tasks" signal. See
# https://cloud.google.com/tasks/docs/creating-http-target-tasks
_CLOUD_TASKS_HEADER = "HTTP_X_CLOUDTASKS_QUEUENAME"

# Header Cloud Scheduler stamps on every job it fires (value "true"). Cloud
# Run likewise strips client-supplied X-CloudScheduler headers, so its presence
# is the "this came from Cloud Scheduler" signal for the run-scheduled handler
# (the CC-213 recurring-sweep clock — a different sender than Cloud Tasks). See
# https://cloud.google.com/scheduler/docs/creating#target
_CLOUD_SCHEDULER_HEADER = "HTTP_X_CLOUDSCHEDULER"


def _reject_if_not_from_cloud_tasks(request):
    """Defence-in-depth: 403 unless the request carries the Cloud Tasks header.

    Returns a ``JsonResponse`` to short-circuit with, or ``None`` to proceed.
    Skipped under TESTING and when CC_TASKS_HANDLER_REQUIRE_HEADER is off, so
    local/in-band invocation and the test-suite can drive the handler
    directly. IAM at the Cloud Run layer is the primary gate; this is a
    secondary check.
    """
    if getattr(settings, "TESTING", False):
        return None
    if not getattr(settings, "CC_TASKS_HANDLER_REQUIRE_HEADER", True):
        return None
    if _CLOUD_TASKS_HEADER not in request.META:
        logger.warning(
            "tasks handler: rejected request missing X-CloudTasks headers"
        )
        return JsonResponse({"error": "forbidden"}, status=403)
    return None


def _reject_if_not_from_cloud_scheduler(request):
    """Defence-in-depth for the run-scheduled handler: 403 unless the request
    carries the X-CloudScheduler header.

    Cloud Scheduler (the CC-213 recurring clock) stamps ``X-CloudScheduler:
    true`` on every fired job — a different sender than Cloud Tasks, so the
    run-scheduled handler validates THAT header, not the queue-name one. Same
    skip conditions (TESTING / CC_TASKS_HANDLER_REQUIRE_HEADER off) as the
    Cloud Tasks guard; IAM at the Cloud Run layer is the primary gate.
    """
    if getattr(settings, "TESTING", False):
        return None
    if not getattr(settings, "CC_TASKS_HANDLER_REQUIRE_HEADER", True):
        return None
    if _CLOUD_SCHEDULER_HEADER not in request.META:
        logger.warning(
            "run-scheduled handler: rejected request missing X-CloudScheduler header"
        )
        return JsonResponse({"error": "forbidden"}, status=403)
    return None


@csrf_exempt
@require_http_methods(["POST"])
def cover_letter_task_handler(request):
    """Run cover-letter generation for a Cloud Tasks-delivered payload.

    Body: ``{"cover_letter_id": <id>, "injected_prompt": <str|null>}`` — the
    exact args the django-q2 ``cover_letter_job`` worker takes. Runs that
    worker synchronously (it re-fetches the pending ``CoverLetter`` row,
    generates, and ``.update()``s content + status), then returns 200.

    Non-2xx => Cloud Tasks retries. We return:
      - 403 if the defence-in-depth Cloud Tasks header guard trips
      - 400 for an unparseable body / missing cover_letter_id (a malformed
        task will never become well-formed, so a retry is pointless — but
        Cloud Tasks caps retries via queue config regardless)
      - 200 once the worker returns, INCLUDING the worker's own
        ``{"status": "missing"|"failed"}`` verdicts: those are terminal,
        already-recorded outcomes (the row was deleted, or generation
        legitimately failed) and must NOT trigger a Cloud Tasks retry.
    """
    guard = _reject_if_not_from_cloud_tasks(request)
    if guard is not None:
        return guard

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    cover_letter_id = payload.get("cover_letter_id")
    if cover_letter_id in (None, ""):
        return JsonResponse({"error": "cover_letter_id required"}, status=400)

    injected_prompt = payload.get("injected_prompt")

    # Same worker the django-q2 path runs. It handles its own errors and
    # updates the durable CoverLetter row; it only re-raises on the AI
    # generation failure (so a transient LLM fault CAN be retried by Cloud
    # Tasks). Import lazily to keep the module import cheap.
    from job_hunting.lib.tasks import cover_letter_job

    result = cover_letter_job(cover_letter_id, injected_prompt=injected_prompt)
    return JsonResponse(result or {"status": "completed"}, status=200)


@csrf_exempt
@require_http_methods(["POST"])
def run_job_task_handler(request):
    """Generic Cloud Tasks handler — run any registered job kind (CC-214).

    Body: ``{"kind": "<kind>", "payload": {...}}``. Dispatches ``kind`` through
    the shared registry (``job_hunting.lib.job_kinds``) to the matching
    ``lib/tasks.py`` worker fn, calling it as ``fn(**payload)`` synchronously —
    the SAME fn the self-host ``run_jobs`` runner calls, so the two transports
    can't drift.

    Non-2xx => Cloud Tasks retries, so this must be safe to run twice. The
    worker fns already re-fetch by pk + ``.update()`` in place (idempotent by
    row identity), the same property the cover-letter path relies on.

    Response codes:
      - 403 if the defence-in-depth Cloud Tasks header guard trips
      - 400 for an unparseable body / missing kind (a malformed task never
        becomes well-formed, so a retry is pointless)
      - 200 for an UNKNOWN kind — terminal: an unregistered kind will never
        become registered by retrying, so 200 avoids a retry storm (logged so
        an operator notices the mismatch)
      - 200 once the worker returns, INCLUDING its own terminal
        ``{"status": "missing"|"failed"}`` verdicts (already-recorded
        outcomes must NOT trigger a retry). Only an exception raised inside
        the worker (e.g. a transient LLM fault) propagates → 500 → retry.
    """
    guard = _reject_if_not_from_cloud_tasks(request)
    if guard is not None:
        return guard

    try:
        body = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    if not isinstance(body, dict):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    kind = body.get("kind")
    if not kind:
        return JsonResponse({"error": "kind required"}, status=400)

    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        return JsonResponse({"error": "payload must be an object"}, status=400)

    from job_hunting.lib.job_kinds import UnknownKind, resolve_kind

    try:
        worker = resolve_kind(kind)
    except UnknownKind:
        # Terminal — an unregistered kind can't self-heal on retry. Log so a
        # producer/registry mismatch is visible, but 200 to stop the retries.
        logger.error("run-job handler: unknown kind %r (no retry)", kind)
        return JsonResponse({"status": "unknown_kind", "kind": kind}, status=200)

    # Worker owns its own error handling + durable row updates; it only
    # re-raises on a retryable fault (which propagates to a 500 → Cloud Tasks
    # retry). Terminal missing/failed verdicts return as 200 below.
    result = worker(**payload)
    return JsonResponse(result or {"status": "completed"}, status=200)


@csrf_exempt
@require_http_methods(["POST"])
def run_scheduled_task_handler(request):
    """Generic Cloud Scheduler handler — run a registered recurring sweep (CC-213).

    Body: ``{"name": "<sweep-name>"}``. Resolves ``name`` through the shared
    ``job_hunting.lib.schedule_kinds.SCHEDULE_REGISTRY`` to the matching sweep
    fn and runs it synchronously — the SAME fn the self-host ``run_jobs`` loop
    runs on cadence, so the two drivers can't drift. The sweeps are the
    recurring django-q2 ``Schedule`` rows the qcluster worker used to own;
    Cloud Tasks is a queue, not a cron, and GCP has no Job runner, so on GCP
    Cloud Scheduler is the clock.

    Sweeps are read/idempotent → at-least-once is safe (this fires alongside
    the still-running CC-199 worker's Schedule until CC-208; the double-fire is
    harmless).

    Same anti-retry-storm idiom as ``/tasks/run-job/``:
      - 403 if the defence-in-depth Cloud Scheduler header guard trips
      - 400 for an unparseable body / missing name
      - 200 for an UNKNOWN name — terminal (an unregistered name won't become
        registered by retrying), logged so an operator notices the mismatch
      - 200 once the sweep returns. Only an exception raised inside the sweep
        propagates → 500 → Cloud Scheduler retry.

    Structured processing logs (parity with the run-job observability): START
    (name), END (duration + result summary), full traceback on an uncaught
    fault, so scheduled runs are visible in Cloud Logging.
    """
    guard = _reject_if_not_from_cloud_scheduler(request)
    if guard is not None:
        return guard

    try:
        body = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    if not isinstance(body, dict):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    name = body.get("name")
    if not name:
        return JsonResponse({"error": "name required"}, status=400)

    from job_hunting.lib.schedule_kinds import UnknownSchedule, resolve_schedule

    try:
        sweep = resolve_schedule(name)
    except UnknownSchedule:
        logger.error(
            "run-scheduled: UNKNOWN sweep name=%r — not registered (no retry)",
            name,
        )
        return JsonResponse({"status": "unknown_schedule", "name": name}, status=200)

    logger.info("run-scheduled: START name=%s", name)
    started = time.monotonic()
    try:
        result = sweep()
    except Exception:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.exception(
            "run-scheduled: FAILED name=%s duration_ms=%.0f — sweep raised",
            name,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "run-scheduled: END name=%s duration_ms=%.0f result=%s",
        name,
        elapsed_ms,
        result,
    )
    # Sweeps return a dict (or an int, for sweep_pending_dispatches) — wrap a
    # non-dict so the response is always a JSON object.
    payload = result if isinstance(result, dict) else {"result": result}
    payload.setdefault("status", "completed")
    return JsonResponse(payload, status=200)
