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


# ---------------------------------------------------------------------------
# Phase 2 — Score + Summary migration
# ---------------------------------------------------------------------------
# Replaces the `threading.Thread` daemon spawns in
# api/job_hunting/api/views/scores.py and summaries.py. Both views now
# enqueue these tasks; the worker container runs them.
#
# Design:
# - Tasks take primary keys (Score / Summary id) and re-fetch inside the
#   body. The Score row already carries job_post_id + resume_id + user_id;
#   the task re-derives `description` and `resume_markdown` from those.
# - Per-request inputs that DON'T live on the row (`injected_prompt`) pass
#   as task kwargs. django-q2 JSON-serializes args; strings are safe.
# - The task ITSELF owns the status-transition contract: on success →
#   `status='completed'`; on exception → `status='failed'`. The exception
#   bubbles to django_q.Failure for operator visibility.
# - AiUsage logging stays in the task because the trigger context
#   (auto vs manual, user_id) is per-call.


def score_job(
    score_id: int,
    *,
    injected_prompt: str | None = None,
    trigger: str = "score",
) -> dict:
    """Run a JobScorer against the JobPost + resume bound to ``score_id``.

    Replaces the daemon-thread bodies in
    ``api/job_hunting/api/views/scores.py``: the helper
    ``_auto_score_job_post`` and the explicit ``ScoreViewSet.create``
    path both enqueue this task.

    Behavior:
    - Re-fetch Score / JobPost / Resume. If the Score row is missing
      (deleted by the user between enqueue and worker pickup), no-op
      and return a sentinel. Cheap defense against the race.
    - Re-derive ``description`` from the JobPost and ``resume_markdown``
      from the Resume (or CareerData when ``resume_id`` is null — the
      auto-score path). The earlier daemon-thread closures captured
      these in-process; the task re-derives so we don't ship 50KB of
      resume markdown through the django_q.OrmQ row.
    - Run the JobScorer; update Score with ``score`` / ``explanation``
      / ``status='completed'``. On exception, set ``status='failed'``
      and re-raise so django_q.Failure captures the traceback.
    - Log AiUsage with the ``trigger`` arg ('score' for explicit, the
      caller may pass 'auto_score' for the helper path).

    Returns the final ``{score, status}`` snapshot for the django_q
    Task row's ``result`` column (useful for /admin/django_q/ filters).
    """
    from job_hunting.lib.ai_client import get_client
    from job_hunting.lib.models import CareerData
    from job_hunting.lib.scoring.job_scorer import JobScorer
    from job_hunting.lib.services.application_prompt_builder import (
        ApplicationPromptBuilder,
    )
    from job_hunting.lib.services.db_export_service import DbExportService
    from job_hunting.models import AiUsage, JobPost, Resume, Score

    score = Score.objects.filter(pk=score_id).first()
    if score is None:
        logger.warning("score_job: score_id=%s no longer exists", score_id)
        return {"score": None, "status": "missing"}

    jp = JobPost.objects.filter(pk=score.job_post_id).first()
    if jp is None or not (jp.description or "").strip():
        Score.objects.filter(pk=score_id).update(status="failed")
        logger.warning(
            "score_job: score_id=%s — JobPost missing or empty description",
            score_id,
        )
        return {"score": None, "status": "failed"}

    if score.resume_id:
        resume = Resume.objects.filter(pk=score.resume_id).first()
        if resume is None:
            Score.objects.filter(pk=score_id).update(status="failed")
            logger.warning("score_job: resume_id=%s missing", score.resume_id)
            return {"score": None, "status": "failed"}
        exporter = DbExportService()
        resume_markdown = exporter.resume_markdown_export(resume)
    else:
        # Auto-score path: no explicit resume, derive from CareerData.
        career_data = CareerData.for_user(score.user_id)
        prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
        resume_markdown = prompt_builder.build_from_career_data(career_data)

    if not (resume_markdown or "").strip():
        Score.objects.filter(pk=score_id).update(status="failed")
        logger.warning(
            "score_job: empty resume markdown for score_id=%s", score_id
        )
        return {"score": None, "status": "failed"}

    client = get_client(required=False)
    if client is None:
        Score.objects.filter(pk=score_id).update(status="failed")
        logger.warning("score_job: no AI client configured")
        return {"score": None, "status": "failed"}

    scorer = JobScorer(client)
    try:
        score_kwargs = {}
        if injected_prompt:
            score_kwargs["injected_prompt"] = injected_prompt
        result = scorer.score_job_match(
            jp.description,
            resume_markdown,
            **score_kwargs,
        )
    except Exception:
        Score.objects.filter(pk=score_id).update(status="failed")
        # Re-raise so django_q.Failure captures the traceback; the row
        # status above is the surface visible to the polling frontend.
        raise

    Score.objects.filter(pk=score_id).update(
        score=result.score,
        explanation=result.evaluation,
        status="completed",
    )

    # AI-usage logging is best-effort — a logging failure must NOT flip
    # the Score back to failed. Same try/except split the daemon-thread
    # bodies used.
    try:
        usage = getattr(result, "_usage", None)
        model_name = getattr(result, "_model_name", "unknown")
        if usage:
            AiUsage.objects.create(
                user_id=score.user_id,
                agent_name="job_scorer",
                model_name=model_name,
                trigger=trigger,
                request_tokens=usage.request_tokens or 0,
                response_tokens=usage.response_tokens or 0,
                total_tokens=usage.total_tokens or 0,
                request_count=usage.requests or 1,
            )
    except Exception:
        logger.exception(
            "score_job: AiUsage logging failed for score_id=%s", score_id
        )

    return {"score": result.score, "status": "completed"}


def summary_job(
    summary_id: int,
    *,
    resume_id: int | None = None,
    injected_prompt: str | None = None,
) -> dict:
    """Generate a Summary for the JobPost + resume bound to ``summary_id``.

    Replaces the daemon-thread body in
    ``api/job_hunting/api/views/summaries.py``: the ``SummaryViewSet.create``
    path enqueues this task when the request is the AI-generation
    branch (manual ``attributes.content`` writes still complete
    synchronously and never enqueue).

    Behavior parallels ``score_job``:
    - Re-fetch Summary; abort if deleted between enqueue and pickup.
    - Re-derive job + resume context. The auto-summarize path (no
      ``resume_id``) builds markdown from CareerData; the explicit
      path uses the resume directly.
    - Run the SummaryService; update content + status; bubble exceptions
      to django_q.Failure on error.
    - Maintain the ResumeSummary single-active-per-resume invariant on
      success, identical to the daemon-thread body.

    Returns ``{status}`` for the django_q Task row.

    Note: Summary has no ``resume_id`` column — the resume link lives
    on ``ResumeSummary``. The view passes ``resume_id`` explicitly so
    the task knows which path (auto vs. resume-bound) to take.
    """
    from job_hunting.lib.ai_client import get_client
    from job_hunting.lib.models import CareerData
    from job_hunting.lib.services.application_prompt_builder import (
        ApplicationPromptBuilder,
    )
    from job_hunting.lib.services.summary_service import SummaryService
    from job_hunting.models import JobPost, Resume, ResumeSummary, Summary

    summary = Summary.objects.filter(pk=summary_id).first()
    if summary is None:
        logger.warning(
            "summary_job: summary_id=%s no longer exists", summary_id
        )
        return {"status": "missing"}

    jp = JobPost.objects.filter(pk=summary.job_post_id).first()
    if jp is None:
        Summary.objects.filter(pk=summary_id).update(status="failed")
        return {"status": "failed"}

    resume = None
    career_markdown = ""
    if resume_id:
        resume = Resume.objects.filter(pk=resume_id).first()
    if resume is None:
        # Auto-summarize path: derive from CareerData.
        career_data = CareerData.for_user(summary.user_id)
        prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
        career_markdown = (
            prompt_builder.build_from_career_data(career_data) or ""
        )
        if not career_markdown.strip():
            Summary.objects.filter(pk=summary_id).update(status="failed")
            return {"status": "failed"}

    client = get_client(required=False)
    if client is None:
        Summary.objects.filter(pk=summary_id).update(status="failed")
        return {"status": "failed"}

    try:
        if resume is None:
            svc = SummaryService(
                client,
                job=jp,
                resume_markdown=career_markdown,
                user_id=summary.user_id,
            )
        else:
            svc = SummaryService(client, job=jp, resume=resume)
        generated_content = svc.generate_content(
            injected_prompt=injected_prompt
        )
    except Exception:
        Summary.objects.filter(pk=summary_id).update(status="failed")
        raise

    Summary.objects.filter(pk=summary_id).update(
        content=generated_content, status="completed"
    )

    # Maintain ResumeSummary's single-active-per-resume invariant when
    # the summary is bound to a resume. Mirrors the daemon-thread body.
    if resume is not None:
        ResumeSummary.objects.filter(resume_id=resume.id).update(
            active=False
        )
        ResumeSummary.objects.get_or_create(
            resume_id=resume.id,
            summary_id=summary_id,
            defaults={"active": True},
        )
        ResumeSummary.objects.filter(
            resume_id=resume.id, summary_id=summary_id
        ).update(active=True)
        ResumeSummary.ensure_single_active_for_resume(resume.id)

    return {"status": "completed"}
