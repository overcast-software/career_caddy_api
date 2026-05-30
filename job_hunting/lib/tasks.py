"""django-q2 task definitions for the Career Caddy backend.

Phase 1 of the django-q2 rollout (Plans/Job-queue integration —
django-q2 phased rollout). This module is the SINGLE import surface
that application code uses to enqueue background work — views and
services call::

    from django_q.tasks import async_task
    async_task("job_hunting.lib.tasks.score_job", score_id)

Conventions enforced here:

- *Pass primary keys, not ORM instances.* The qcluster process serializes
  task args; instance pickling is unreliable across worker restarts and
  database migrations. Tasks fetch fresh inside their body so the work
  always operates on the row's current state.
- *No DRF / view machinery.* Tasks call into the same service layer
  (lib/services/*.py, lib/ai_client.py, lib/job_post_extractor.py) that
  the daemon-thread bodies called into. The view's responsibility is
  bookkeeping + enqueue; the task's responsibility is the work itself.
- *Failures bubble up.* django-q2 catches exceptions and writes them to
  django_q.Failure with the traceback. Don't catch-and-log here — the
  built-in failure surface is the audit trail.
- *Idempotency where cheap.* Tasks that update a row should tolerate
  being re-enqueued (a poller retry, a manual requeue): re-fetch, check
  status, no-op if already terminal.

Phases that follow this one populate this module:
- Phase 2: ``score_job``, ``summary_job``
- Phase 3: ``cover_letter_job``, ``answer_job``, ``question_job``, ``resume_parse_job``
- Phase 4: ``scrape_job`` (replaces the hold-poller)
- Phase 5: ``parse_scrape_job`` (replaces the extraction daemon thread)
            + ``score_pending_posts`` (scheduled, replaces score-poller)
- Phase 7: ``dispatch_federation_activity`` (ActivityPub 5d outbound)

For Phase 1, only the smoke-test task lives here.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def health_check(message: str | None = None) -> dict:
    """Smoke-test task that confirms the qcluster process is wired up.

    Usage from a Django shell::

        from django_q.tasks import async_task
        task_id = async_task("job_hunting.lib.tasks.health_check", "hello")

    The qcluster worker picks the task up off the django_q_ormq queue
    table and runs this function. The return value is persisted on the
    django_q.Task row so the caller (or a future poller) can read it
    back via ``fetch(task_id)``.

    No side effects — no DB writes, no LLM calls, no scrape graph. The
    point is to verify the worker / broker / settings plumbing works
    before any real task migrates onto the queue in Phase 2+.
    """
    payload = {
        "ok": True,
        "message": message or "health_check ran",
        "ts": time.time(),
    }
    logger.info("django-q2 health_check executed: %s", payload)
    return payload
