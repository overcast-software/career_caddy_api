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

from job_hunting.lib import events

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

    user_id = score.user_id

    jp = JobPost.objects.filter(pk=score.job_post_id).first()
    if jp is None or not (jp.description or "").strip():
        Score.objects.filter(pk=score_id).update(status="failed")
        events.notify("score", score_id, "failed", user_id)
        logger.warning(
            "score_job: score_id=%s — JobPost missing or empty description",
            score_id,
        )
        return {"score": None, "status": "failed"}

    if score.resume_id:
        resume = Resume.objects.filter(pk=score.resume_id).first()
        if resume is None:
            Score.objects.filter(pk=score_id).update(status="failed")
            events.notify("score", score_id, "failed", user_id)
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
        events.notify("score", score_id, "failed", user_id)
        logger.warning(
            "score_job: empty resume markdown for score_id=%s", score_id
        )
        return {"score": None, "status": "failed"}

    client = get_client(required=False)
    if client is None:
        Score.objects.filter(pk=score_id).update(status="failed")
        events.notify("score", score_id, "failed", user_id)
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
        events.notify("score", score_id, "failed", user_id)
        # Re-raise so django_q.Failure captures the traceback; the row
        # status above is the surface visible to the polling frontend.
        raise

    Score.objects.filter(pk=score_id).update(
        score=result.score,
        explanation=result.evaluation,
        status="completed",
    )
    events.notify("score", score_id, "completed", user_id)

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

    user_id = summary.user_id

    jp = JobPost.objects.filter(pk=summary.job_post_id).first()
    if jp is None:
        Summary.objects.filter(pk=summary_id).update(status="failed")
        events.notify("summary", summary_id, "failed", user_id)
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
            events.notify("summary", summary_id, "failed", user_id)
            return {"status": "failed"}

    client = get_client(required=False)
    if client is None:
        Summary.objects.filter(pk=summary_id).update(status="failed")
        events.notify("summary", summary_id, "failed", user_id)
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
        events.notify("summary", summary_id, "failed", user_id)
        raise

    Summary.objects.filter(pk=summary_id).update(
        content=generated_content, status="completed"
    )
    events.notify("summary", summary_id, "completed", user_id)

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


# ---------------------------------------------------------------------------
# Phase 3 — Cover Letter / Answer / Resume migrations
# ---------------------------------------------------------------------------
# Three more daemon-thread spawn points retire here: cover_letters.create,
# questions.AnswerViewSet.create (the ai_assist branch), and
# resumes.ResumeViewSet.ingest. The Question/Answer split is one task —
# the threading site lives in questions.py but writes Answer rows.
#
# Resume specifically passes the uploaded file_blob through the queue as a
# task kwarg. django_q2 serializes args via pickle; bytes are safe. A
# proper blob store (s3 or shared volume) is a follow-up — for now,
# OrmQ rows for resume tasks will be ~size-of-uploaded-file (resumes
# average ~100KB–2MB, well under Postgres TOAST limits).


def cover_letter_job(
    cover_letter_id: int,
    *,
    injected_prompt: str | None = None,
) -> dict:
    """Generate a CoverLetter for the JobPost + resume bound to ``cover_letter_id``.

    Replaces the daemon-thread body in
    ``api/job_hunting/api/views/cover_letters.py``: the ``CoverLetterViewSet.create``
    path enqueues this task when the request is the AI-generation
    branch (manual ``attributes.content`` writes still complete
    synchronously and never enqueue).

    The CoverLetter row already carries ``user_id`` / ``resume_id`` /
    ``job_post_id`` from the view's row-creation step, so the task
    re-fetches by pk and re-derives context.
    """
    from job_hunting.lib.ai_client import get_client
    from job_hunting.lib.models import CareerData
    from job_hunting.lib.services.application_prompt_builder import (
        ApplicationPromptBuilder,
    )
    from job_hunting.lib.services.cover_letter_service import CoverLetterService
    from job_hunting.models import CoverLetter, JobPost, Resume

    cl = CoverLetter.objects.filter(pk=cover_letter_id).first()
    if cl is None:
        logger.warning(
            "cover_letter_job: cover_letter_id=%s no longer exists",
            cover_letter_id,
        )
        return {"status": "missing"}

    user_id = cl.user_id

    jp = JobPost.objects.filter(pk=cl.job_post_id).first()
    if jp is None:
        CoverLetter.objects.filter(pk=cover_letter_id).update(status="failed")
        events.notify("cover_letter", cover_letter_id, "failed", user_id)
        return {"status": "failed"}

    resume = (
        Resume.objects.filter(pk=cl.resume_id).first() if cl.resume_id else None
    )

    career_markdown = ""
    if resume is None:
        career_data = CareerData.for_user(cl.user_id)
        prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
        career_markdown = (
            prompt_builder.build_from_career_data(career_data) or ""
        )
        if not career_markdown.strip():
            CoverLetter.objects.filter(pk=cover_letter_id).update(
                status="failed"
            )
            events.notify("cover_letter", cover_letter_id, "failed", user_id)
            return {"status": "failed"}

    client = get_client(required=False)
    if client is None:
        CoverLetter.objects.filter(pk=cover_letter_id).update(status="failed")
        events.notify("cover_letter", cover_letter_id, "failed", user_id)
        return {"status": "failed"}

    try:
        svc = CoverLetterService(
            client,
            jp,
            resume=resume,
            resume_markdown=career_markdown if resume is None else None,
            user_id=cl.user_id,
        )
        gen_kwargs = {}
        if injected_prompt:
            gen_kwargs["injected_prompt"] = injected_prompt
        generated_content = svc.generate_cover_letter(**gen_kwargs)
    except Exception:
        CoverLetter.objects.filter(pk=cover_letter_id).update(status="failed")
        events.notify("cover_letter", cover_letter_id, "failed", user_id)
        raise

    CoverLetter.objects.filter(pk=cover_letter_id).update(
        content=generated_content, status="completed"
    )
    events.notify("cover_letter", cover_letter_id, "completed", user_id)
    return {"status": "completed"}


def answer_job(
    answer_id: int,
    *,
    injected_prompt: str | None = None,
    resume_id: int | None = None,
) -> dict:
    """Generate an AI Answer for the Question bound to ``answer_id``.

    Replaces the daemon-thread body in
    ``api/job_hunting/api/views/questions.py``'s answer-creation path
    (the ``ai_assist=True`` branch).

    The Answer row already carries ``question_id``; the task re-fetches
    both. Resume context is per-call (not on the Answer row) so it
    passes via kwarg. ``resume_id=None`` means use the user's
    CareerData; ``resume_id`` set means use that specific resume's
    exported markdown.
    """
    from job_hunting.lib.ai_client import get_client
    from job_hunting.lib.models import CareerData
    from job_hunting.lib.services.answer_service import AnswerService
    from job_hunting.lib.services.application_prompt_builder import (
        ApplicationPromptBuilder,
    )
    from job_hunting.lib.services.db_export_service import DbExportService
    from job_hunting.models import Answer, Question, Resume

    answer = Answer.objects.filter(pk=answer_id).first()
    if answer is None:
        logger.warning("answer_job: answer_id=%s no longer exists", answer_id)
        return {"status": "missing"}

    question = Question.objects.filter(pk=answer.question_id).first()
    if question is None:
        Answer.objects.filter(pk=answer_id).update(status="failed")
        events.notify("answer", answer_id, "failed", None)
        return {"status": "failed"}

    # Answer has no user FK; derive from the owning question. user_id model
    # may live as `user_id` or `created_by_id` depending on Question's
    # current schema — accept either.
    user_id = getattr(question, "user_id", None) or getattr(
        question, "created_by_id", None
    )

    # Re-derive career markdown from resume_id or CareerData.
    career_markdown = ""
    if resume_id:
        resume = Resume.objects.filter(pk=resume_id).first()
        if resume is None:
            Answer.objects.filter(pk=answer_id).update(status="failed")
            events.notify("answer", answer_id, "failed", user_id)
            return {"status": "failed"}
        career_markdown = (
            DbExportService().resume_markdown_export(resume) or ""
        )
    elif user_id is not None:
        career_data = CareerData.for_user(user_id)
        prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
        career_markdown = (
            prompt_builder.build_from_career_data(career_data) or ""
        )

    client = get_client(required=False)
    if client is None:
        Answer.objects.filter(pk=answer_id).update(status="failed")
        events.notify("answer", answer_id, "failed", user_id)
        return {"status": "failed"}

    try:
        svc = AnswerService(client)
        gen_kwargs = {
            "question": question,
            "save": False,
            "injected_prompt": injected_prompt,
        }
        if career_markdown:
            gen_kwargs["career_markdown"] = career_markdown
        result = svc.generate_answer(**gen_kwargs)
        generated_content = (
            result.content if isinstance(result, Answer) else str(result or "")
        )
    except Exception:
        Answer.objects.filter(pk=answer_id).update(status="failed")
        events.notify("answer", answer_id, "failed", user_id)
        raise

    Answer.objects.filter(pk=answer_id).update(
        content=generated_content, status="completed"
    )
    events.notify("answer", answer_id, "completed", user_id)
    return {"status": "completed"}


def resume_parse_job(
    resume_id: int,
    *,
    file_blob: bytes,
    resume_name: str,
    derived_name: str | None = None,
) -> dict:
    """Parse an uploaded resume file and populate the Resume row.

    Replaces the daemon-thread body in
    ``api/job_hunting/api/views/resumes.py``'s ingest endpoint, including
    the bespoke ``threading.Thread.join(timeout=300)`` ceiling — the
    Q_CLUSTER ``timeout: 300`` setting now owns that contract, and
    django_q.Failure will surface a timeout as a normal failure with the
    traceback.

    ``file_blob`` rides through the OrmQ row as pickle-serialized
    bytes. Resume uploads average ~100KB–2MB which is fine for OrmQ;
    a proper blob store (s3 or shared volume) is a Phase 6+ follow-up.
    """
    from django.contrib.auth import get_user_model

    from job_hunting.lib.services.ingest_resume import IngestResume
    from job_hunting.models import Resume

    resume = Resume.objects.filter(pk=resume_id).first()
    if resume is None:
        logger.warning(
            "resume_parse_job: resume_id=%s no longer exists", resume_id
        )
        return {"status": "missing"}

    user_id = resume.user_id

    User = get_user_model()
    user = User.objects.filter(pk=resume.user_id).first() if resume.user_id else None
    if user is None:
        Resume.objects.filter(pk=resume_id).update(status="failed")
        events.notify("resume", resume_id, "failed", user_id)
        return {"status": "failed"}

    try:
        ingest_service = IngestResume(
            user=user,
            resume=file_blob,
            resume_name=resume_name,
            agent=None,
            db_resume=resume,
        )
        ingest_service.process()
    except Exception:
        Resume.objects.filter(pk=resume_id).update(status="failed")
        events.notify("resume", resume_id, "failed", user_id)
        raise

    r = Resume.objects.filter(pk=resume_id).first()
    if r:
        if not r.title and derived_name:
            r.title = derived_name
        r.status = "completed"
        r.save()
    events.notify("resume", resume_id, "completed", user_id)
    return {"status": "completed"}


# ---------------------------------------------------------------------------
# Phase 5a — parse_scrape migration
# ---------------------------------------------------------------------------
# Replaces the `threading.Thread` daemon spawn at the tail of
# `parse_scrape` in job_hunting/lib/parsers/job_post_extractor.py. The
# task target re-enters parse_scrape with `sync=True` so the existing
# pipeline body (tier-0/1/2/3 fallback, ScrapeProfile update,
# CompletenessReviewer gate, scrape status logging) runs inline inside
# the qcluster worker.


def parse_scrape_job(
    scrape_id: int,
    *,
    user_id: int | None = None,
    force: bool = False,
) -> dict:
    """Run parse_scrape inside the qcluster worker.

    Tier-2/3 LLM fallbacks routinely take ~30–90 seconds and the
    pipeline can re-attempt CompletenessReviewer + ScrapeProfile
    update on top of that. Worker timeout is Q_CLUSTER.timeout=300
    (5 min) — long-running parses get the full ceiling.
    """
    from job_hunting.lib.parsers.job_post_extractor import parse_scrape

    parse_scrape(scrape_id, user_id=user_id, sync=True, force=force)
    return {"scrape_id": scrape_id, "status": "completed"}


# ---------------------------------------------------------------------------
# Phase 2 of Plans/Scrape runner — lease timeout sweep
# ---------------------------------------------------------------------------
# Crash recovery for the scrape runner. If a runner picks up a Scrape via
# POST /scrapes/claim-next/ and then dies (kill -9, OOM, network split, host
# reboot), the row stays at status='running' (or 'extracting',
# 'updating_profile', …) with a stale `claimed_at` set. Nothing else picks it
# back up because the claim endpoint only sees status='hold'.
#
# The runner heartbeats `claimed_at = NOW()` on each non-terminal status
# write inside `_log_scrape_status` (shipped Phase 1). This sweep resets any
# row whose claim is older than the threshold AND status is still
# non-terminal — that combination identifies a runner that picked up work
# but stopped checking in. The row goes back to 'hold' for the next claim.
#
# Schedule: registered as a django-q2 Schedule (`schedule_type='I'`,
# minutes=5) by migration 0086. Idempotent; safe to re-run on every deploy.
# The task itself is idempotent too — runs on a `claimed_at < cutoff`
# filter, so running it twice in a row only resets what's already stale.


# Non-terminal scrape statuses the sweep covers. Anything else (None,
# 'hold', 'completed', 'failed') is either already-available-to-claim or
# already-finished and not a candidate for reset.
_SWEEPABLE_STATUSES = (
    "running",
    "extracting",
    "updating_profile",
    "resolving_apply_url",
    "navigating",
    "resolveapplyurl",
)

# Default lease window. Tier-3 LLM fallbacks + browser load can hit ~5-7 min
# on a slow profile, and the heartbeat fires on each status update — 15 min
# is comfortable headroom for a healthy long-running scrape without leaving
# crashed claims wedged for hours.
_DEFAULT_LEASE_MINUTES = 15


def sweep_stale_scrape_claims(threshold_minutes: int = _DEFAULT_LEASE_MINUTES) -> dict:
    """Reset Scrape rows whose runner claim has gone stale.

    A row is stale when:
    - ``claimed_at`` is older than ``threshold_minutes`` ago AND
    - ``status`` is non-terminal (still appears to be "in progress")

    The reset clears ``claimed_at`` + ``claimed_by`` and flips ``status``
    back to ``'hold'``. The next runner that polls ``POST /scrapes/claim-
    next/`` will pick it back up.

    Returns ``{reset: N, threshold_minutes: M, cutoff: iso}`` for the
    django_q.Task row so operators can grep the schedule history. A
    warning is logged per reset row with the prior claimant for blame
    attribution.
    """
    from datetime import timedelta

    from django.utils import timezone

    from job_hunting.models import Scrape

    cutoff = timezone.now() - timedelta(minutes=threshold_minutes)

    # Snapshot the candidate rows first so we can log each reset with the
    # prior claimant (for runner blame attribution in logfire). The .values
    # avoids hydrating full Scrape instances for what's a small bookkeeping
    # set in steady state.
    stale = list(
        Scrape.objects.filter(
            claimed_at__lt=cutoff,
            status__in=_SWEEPABLE_STATUSES,
        ).values("id", "claimed_by", "claimed_at", "status")
    )

    if not stale:
        return {
            "reset": 0,
            "threshold_minutes": threshold_minutes,
            "cutoff": cutoff.isoformat(),
        }

    # Bulk reset. Identical semantics to per-row .save() but one round-trip.
    reset_count = Scrape.objects.filter(
        id__in=[row["id"] for row in stale]
    ).update(
        status="hold",
        claimed_at=None,
        claimed_by=None,
    )

    for row in stale:
        logger.warning(
            "scrape claim swept: id=%s prior_claimant=%s prior_status=%s "
            "claimed_at=%s (stale > %sm)",
            row["id"],
            row["claimed_by"],
            row["status"],
            row["claimed_at"].isoformat() if row["claimed_at"] else None,
            threshold_minutes,
        )

    return {
        "reset": reset_count,
        "threshold_minutes": threshold_minutes,
        "cutoff": cutoff.isoformat(),
    }


# ---------------------------------------------------------------------------
# ScrapeProfile sharpen — staff-triggered enhancer pass
# ---------------------------------------------------------------------------
# Triggered by POST /api/v1/scrape-profiles/:id/sharpen/. The endpoint
# enqueues this task and returns 202 with the job_id so the staff curator
# (or the eventual /admin frontend button) can fire-and-forget.
#
# Integration with the agents-side `scrape-profile-enhancer` flow lives in
# `agents/` and is NOT importable from the api container — the
# `agents/` submodule ships as its own image (browser/Camoufox runtime).
# Until a runnable enhancer driver lands (subprocess shell-out, MCP RPC,
# or a runner-claimed work queue analogous to scrape-claim-next), this
# task's body is a recorded intent: it persists the request snapshot
# (timestamp, requester, source scrape) onto the profile so the offline
# enhancer pass has a queue to walk, and the audit row tells staff the
# request landed. The line marked `# ENHANCER INTEGRATION POINT` is
# where the real driver call goes when one exists.
#
# This split keeps the api/agents Python boundary intact (no cross-image
# imports) while giving the frontend a working button to wire today.


def sharpen_scrape_profile(
    profile_id: int,
    *,
    source_scrape_id: int,
    requested_by_id: int | None = None,
) -> dict:
    """Record a sharpen request against a ScrapeProfile + source Scrape.

    The task body re-fetches the profile and source scrape, records the
    request onto the profile's metadata (extraction_hints log line +
    timestamps), and emits a structured log line the offline enhancer
    pass / future runner can pick up. The actual selector / hint
    rewriting lives in the agents-side enhancer; this task is the api's
    half of the contract.

    Returns ``{profile_id, source_scrape_id, status}`` for the
    django_q Task row's ``result`` column.
    """
    from django.utils import timezone

    from job_hunting.models import Scrape, ScrapeProfile

    profile = ScrapeProfile.objects.filter(pk=profile_id).first()
    if profile is None:
        logger.warning(
            "sharpen_scrape_profile: profile_id=%s no longer exists",
            profile_id,
        )
        return {
            "profile_id": profile_id,
            "source_scrape_id": source_scrape_id,
            "status": "missing",
        }

    source_scrape = Scrape.objects.filter(pk=source_scrape_id).first()
    if source_scrape is None:
        logger.warning(
            "sharpen_scrape_profile: source_scrape_id=%s no longer exists",
            source_scrape_id,
        )
        return {
            "profile_id": profile_id,
            "source_scrape_id": source_scrape_id,
            "status": "source_missing",
        }

    # ENHANCER INTEGRATION POINT — the agents-side scrape-profile-enhancer
    # subagent runs out-of-process today (Claude subagent, operator-driven).
    # When a runnable driver lands (MCP tool, subprocess shell-out, or a
    # runner-claimed work queue), invoke it here. For now we record the
    # request so the offline pass can walk it.
    now = timezone.now()
    existing_hints = profile.extraction_hints or ""
    hint_line = (
        f"\n[sharpen-request {now.isoformat()}] "
        f"requested_by={requested_by_id or 'anonymous'} "
        f"source_scrape={source_scrape_id}"
    )
    profile.extraction_hints = existing_hints + hint_line
    profile.save(update_fields=["extraction_hints", "updated_at"])

    logger.info(
        "sharpen_scrape_profile: profile=%s hostname=%s source_scrape=%s "
        "requested_by=%s — request recorded; awaiting enhancer pass",
        profile.id,
        profile.hostname,
        source_scrape_id,
        requested_by_id,
    )

    return {
        "profile_id": profile_id,
        "source_scrape_id": source_scrape_id,
        "hostname": profile.hostname,
        "status": "requested",
    }
