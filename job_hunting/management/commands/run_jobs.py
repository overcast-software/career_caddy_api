"""Self-host async runner — drain the Job queue (CC-214).

The drop-in replacement for ``manage.py qcluster`` on self-host / local
deployments (``CC_TASKS_ENABLED`` off). It claims ``Job`` rows with the SAME
``SELECT FOR UPDATE SKIP LOCKED`` discipline as ``/scrapes/claim-next/``,
dispatches each by ``kind`` through the shared registry
(``job_hunting.lib.job_kinds``) to its ``lib/tasks.py`` worker fn, and marks
the row completed/failed. N runners coexist safely (SKIP LOCKED).

This runner is self-host-ONLY — it is never a GCP service (the GCP worker is
sunset; on GCP the transport is Cloud Tasks and no Job row is ever written).

Usage::

    python manage.py run_jobs                      # loop forever
    python manage.py run_jobs --once               # drain then exit (tests/CI)
    python manage.py run_jobs --runner-name pibu   # claim attribution
    python manage.py run_jobs --poll 4             # idle sleep seconds
    python manage.py run_jobs --lease-minutes 15   # stale-claim reset window
"""

from __future__ import annotations

import logging
import socket
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Drain the self-host Job queue (CC-214). qcluster's replacement."

    def add_arguments(self, parser):
        parser.add_argument(
            "--runner-name",
            default=None,
            help="Claim attribution; defaults to the hostname.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain everything currently claimable, then exit.",
        )
        parser.add_argument(
            "--poll",
            type=int,
            default=4,
            help="Seconds to sleep when the queue is empty (loop mode).",
        )
        parser.add_argument(
            "--lease-minutes",
            type=int,
            default=15,
            help="Reset a claim as stale after this many minutes.",
        )
        parser.add_argument(
            "--sweep-every",
            type=int,
            default=60,
            help="Seconds between stale-claim sweeps (loop mode).",
        )

    def handle(self, *args, runner_name, once, poll, lease_minutes, sweep_every, **opts):
        from job_hunting.models import Job

        runner = runner_name or socket.gethostname() or "run_jobs"
        self.stdout.write(f"run_jobs: runner={runner} once={once} poll={poll}s")

        last_sweep = 0.0
        while True:
            # Periodic lease recovery so a crashed sibling's stuck claims come
            # back. Cheap + idempotent; runs on a cadence, not every claim.
            now = time.monotonic()
            if now - last_sweep >= sweep_every:
                result = Job.objects.sweep_stale_claims(threshold_minutes=lease_minutes)
                if result["reset"] or result["failed"]:
                    self.stdout.write(
                        f"run_jobs: swept reset={result['reset']} "
                        f"failed={result['failed']}"
                    )
                last_sweep = now

            job = Job.objects.claim_next(runner_name=runner)
            if job is None:
                if once:
                    # A single sweep-then-claim can miss a row a sibling had
                    # locked; but --once is for tests/CI where the queue is
                    # controlled, so an empty claim means done.
                    self.stdout.write("run_jobs: queue drained")
                    return
                time.sleep(poll)
                continue

            self._run_one(job)

    def _run_one(self, job) -> None:
        """Dispatch one claimed Job by kind and record the terminal status."""
        from job_hunting.lib.job_kinds import (
            NON_COMPLETED_VERDICTS,
            UnknownKind,
            job_ref as _job_ref,
            resolve_kind,
        )
        from job_hunting.models import Job

        try:
            worker = resolve_kind(job.kind)
        except UnknownKind:
            logger.error(
                "run_jobs: job=%s unknown kind %r -> failed", job.id, job.kind
            )
            Job.objects.filter(pk=job.id).update(
                status="failed", claimed_at=None, claimed_by=None
            )
            return

        payload = job.payload if isinstance(job.payload, dict) else {}
        job_ref = _job_ref(payload)

        # Structured processing logs so the operator can SEE jobs being
        # processed (parity with the Cloud Tasks /tasks/run-job/ handler).
        logger.info("run_jobs: START job=%s kind=%s %s", job.id, job.kind, job_ref)
        started = time.monotonic()
        try:
            result = worker(**payload)
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000
            # The worker already recorded its own durable failure row (Score
            # status='failed', etc.); it re-raises only on a retryable fault.
            # Mark the Job failed + release the claim so the sweep can requeue
            # it if attempts remain.
            logger.exception(
                "run_jobs: FAILED job=%s kind=%s %s duration_ms=%.0f — worker raised",
                job.id,
                job.kind,
                job_ref,
                elapsed_ms,
            )
            Job.objects.filter(pk=job.id).update(
                status="failed", claimed_at=None, claimed_by=None
            )
            return

        elapsed_ms = (time.monotonic() - started) * 1000
        verdict = result.get("status") if isinstance(result, dict) else None
        # A non-completed terminal verdict (missing/failed/…) is not a Job
        # failure — the worker chose to no-op — but it means no result row was
        # produced, so surface it at WARNING rather than leaving it silent.
        if verdict in NON_COMPLETED_VERDICTS:
            logger.warning(
                "run_jobs: END job=%s kind=%s %s duration_ms=%.0f verdict=%s "
                "(terminal — no result row produced)",
                job.id,
                job.kind,
                job_ref,
                elapsed_ms,
                verdict,
            )
        else:
            logger.info(
                "run_jobs: END job=%s kind=%s %s duration_ms=%.0f verdict=%s",
                job.id,
                job.kind,
                job_ref,
                elapsed_ms,
                verdict or "completed",
            )

        Job.objects.filter(pk=job.id).update(
            status="completed",
            claimed_at=timezone.now(),
        )
