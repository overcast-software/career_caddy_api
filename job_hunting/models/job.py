"""Generic async Job — the self-host / local pull queue (CC-214).

The unified async-dispatch model has a single ``enqueue(kind, **payload)``
producer whose transport is chosen per deployment by ``CC_TASKS_ENABLED``:

- **GCP (`CC_TASKS_ENABLED=True`)**: a Cloud Task is created and a generic
  ``/tasks/run-job/`` handler runs the matching ``lib/tasks.py`` fn. No Job
  row is written; no worker/runner exists on GCP; Cloud Tasks' own managed
  retry is the safety net.
- **Self-host / local (`CC_TASKS_ENABLED` off)**: a ``Job`` row is written
  and a generic ``manage.py run_jobs`` runner drains it, claiming rows with
  the SAME ``SELECT FOR UPDATE SKIP LOCKED`` discipline as
  ``scrapes.py:claim_next``.

This is django-q2's replacement for the self-host case. It exists ONLY where
there is no Cloud Tasks — it is NEVER a GCP service (the GCP worker is sunset,
see claudex ``qcluster-worker-is-temporary-bridge-must-be-removed``).

``kind`` selects a ``lib/tasks.py`` worker fn via the shared kind registry
(``job_hunting.lib.job_kinds.KIND_REGISTRY``); ``payload`` carries PKs + small
scalars only (same convention as the django-q2 call sites), never blobs.
"""

from __future__ import annotations

import logging

from django.db import models

from .base import GetMixin
from .nanoid_pk import NanoIDModel

logger = logging.getLogger(__name__)

# Non-terminal statuses a claim can be stuck in. A row in one of these with a
# stale ``claimed_at`` is a crashed runner and gets reset by the lease sweep.
_SWEEPABLE_STATUSES = ("pending", "running")


class JobManager(models.Manager):
    """Manager holding the atomic claim + lease-sweep helpers.

    ``claim_next`` mirrors ``ScrapeViewSet.claim_next`` exactly: SKIP LOCKED
    so N coexisting runners never grab the same row, a bounded transaction
    (SET LOCAL lock_timeout/statement_timeout) so a claim can never pin a
    worker, FIFO ``created_at ASC NULLS FIRST, id``, a ``run_after`` gate for
    delayed / recurring jobs, and contention → ``None`` (the runner just
    retries on its next poll).
    """

    def claim_next(self, runner_name: str = "anonymous"):
        """Atomically claim the oldest runnable pending Job, or return None.

        A Job is runnable when ``status='pending'`` AND ``claimed_at IS NULL``
        AND (``run_after IS NULL`` OR ``run_after <= now``). The claimed row is
        flipped to ``status='running'`` with ``claimed_at=now`` /
        ``claimed_by=runner_name`` and returned; ``None`` means nothing is
        claimable right now (empty queue OR transient contention) — identical
        to the scrapes claim's 204.
        """
        from django.db import OperationalError, connection, transaction
        from django.utils import timezone

        try:
            with transaction.atomic():
                # Bound the txn so a single claim can NEVER pin a worker for
                # the full request/loop timeout (the scrapes CC-96 wedge fix).
                # SET LOCAL resets on commit/rollback.
                with connection.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '3s'")
                    cur.execute("SET LOCAL statement_timeout = '5s'")

                now = timezone.now()
                claimable = (
                    self.select_for_update(skip_locked=True)
                    .filter(
                        status="pending",
                        claimed_at__isnull=True,
                    )
                    # run_after gate: NULL (immediate) or already due.
                    .filter(
                        models.Q(run_after__isnull=True)
                        | models.Q(run_after__lte=now)
                    )
                    # FIFO by creation time; pre-existing NULL created_at rows
                    # sort first as the oldest, id is the stable tiebreak.
                    # Served by job_claim_queue_idx (mirrors
                    # scrape_claim_queue_idx).
                    .order_by(
                        models.F("created_at").asc(nulls_first=True), "id"
                    )
                    .first()
                )
                if claimable is None:
                    return None

                claimable.status = "running"
                claimable.claimed_at = now
                claimable.claimed_by = runner_name
                claimable.attempts = (claimable.attempts or 0) + 1
                claimable.save(
                    update_fields=[
                        "status",
                        "claimed_at",
                        "claimed_by",
                        "attempts",
                    ]
                )
                return claimable
        except OperationalError as exc:
            # lock_timeout (55P03) / statement_timeout (57014) / transient
            # contention → "nothing claimable right now"; the aborted txn
            # releases its locks cleanly so no stuck idle-in-transaction
            # backend forms. The runner sleeps + retries.
            logger.warning(
                "job claim contended; returning None (runner=%s): %s",
                runner_name,
                exc,
            )
            return None

    def sweep_stale_claims(self, threshold_minutes: int = 15) -> dict:
        """Reset Jobs whose runner claim has gone stale (crash recovery).

        A Job is stale when ``claimed_at`` is older than ``threshold_minutes``
        AND ``status`` is non-terminal (``pending``/``running``). The reset
        clears ``claimed_at``/``claimed_by`` and flips status back to
        ``pending`` so the next ``claim_next`` picks it up. Mirrors
        ``lib/tasks.py:sweep_stale_scrape_claims``.

        A row that has burned through ``max_attempts`` is not requeued; it is
        marked ``failed`` (a crashed runner already consumed the attempt in
        ``claim_next``), so a poison job can't loop forever.

        Returns ``{reset, failed, threshold_minutes, cutoff}``.
        """
        from datetime import timedelta

        from django.utils import timezone

        cutoff = timezone.now() - timedelta(minutes=threshold_minutes)
        stale = list(
            self.filter(
                claimed_at__lt=cutoff,
                status__in=_SWEEPABLE_STATUSES,
            ).values("id", "claimed_by", "claimed_at", "status", "attempts", "max_attempts")
        )
        if not stale:
            return {
                "reset": 0,
                "failed": 0,
                "threshold_minutes": threshold_minutes,
                "cutoff": cutoff.isoformat(),
            }

        requeue_ids = [
            r["id"] for r in stale if (r["attempts"] or 0) < (r["max_attempts"] or 1)
        ]
        exhausted_ids = [
            r["id"] for r in stale if (r["attempts"] or 0) >= (r["max_attempts"] or 1)
        ]

        reset_count = 0
        if requeue_ids:
            reset_count = self.filter(id__in=requeue_ids).update(
                status="pending",
                claimed_at=None,
                claimed_by=None,
            )
        failed_count = 0
        if exhausted_ids:
            failed_count = self.filter(id__in=exhausted_ids).update(
                status="failed",
                claimed_at=None,
                claimed_by=None,
            )

        for row in stale:
            logger.warning(
                "job claim swept: id=%s kind-row prior_claimant=%s "
                "prior_status=%s attempts=%s/%s claimed_at=%s (stale > %sm)",
                row["id"],
                row["claimed_by"],
                row["status"],
                row["attempts"],
                row["max_attempts"],
                row["claimed_at"].isoformat() if row["claimed_at"] else None,
                threshold_minutes,
            )

        return {
            "reset": reset_count,
            "failed": failed_count,
            "threshold_minutes": threshold_minutes,
            "cutoff": cutoff.isoformat(),
        }


class Job(GetMixin, NanoIDModel):
    """A unit of self-host async work: dispatch ``kind`` with ``payload``.

    ``id`` is the 10-char NanoID PK from NanoIDModel (house convention for
    new models). Only written on the self-host / local transport; on GCP the
    equivalent is a Cloud Task and no Job row is created.
    """

    STATUS_CHOICES = (
        ("pending", "pending"),
        ("running", "running"),
        ("completed", "completed"),
        ("failed", "failed"),
    )

    # Which lib/tasks.py worker fn to dispatch — resolved through the shared
    # kind registry (job_hunting.lib.job_kinds). NOT a dotted path on the row
    # (keeps payloads opaque + prevents an untrusted row naming an arbitrary
    # importable).
    kind = models.CharField(max_length=64)
    # PKs + small scalars the worker fn needs; never blobs (same convention
    # as the django-q2 async_task args this replaces).
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True
    )

    # Atomic-claim bookkeeping — mirrors Scrape.claimed_at/claimed_by. Reset
    # to NULL by the lease sweep when a claim goes stale.
    claimed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    claimed_by = models.CharField(max_length=100, null=True, blank=True)

    # FIFO arrival key for the claim queue (NanoID PK can't stand in for it).
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    # Delay / schedule gate: a Job is not claimable until run_after <= now.
    # NULL means "runnable immediately". Absorbs django-q2's delayed tasks +
    # the recurring-sweep cron (a sweep re-enqueues itself with a future
    # run_after) into one column. On GCP those instead run via Cloud
    # Scheduler → the handler (CC-213).
    run_after = models.DateTimeField(null=True, blank=True, db_index=True)

    # Attempt accounting: claim_next increments attempts; the lease sweep
    # fails a row once attempts >= max_attempts so a poison job can't loop.
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=1)

    objects = JobManager()

    class Meta:
        db_table = "job"
        indexes = [
            # Partial composite index backing claim_next (mirrors
            # scrape_claim_queue_idx). The claim query is
            #   WHERE status='pending' AND claimed_at IS NULL
            #     AND (run_after IS NULL OR run_after <= now)
            #   ORDER BY created_at ASC NULLS FIRST, id
            #   LIMIT 1 FOR UPDATE SKIP LOCKED
            # The condition prunes the index to just the active pending queue;
            # leading with the ORDER BY columns keeps it index-only. run_after
            # is carried as a trailing column so the due-gate is served from
            # the same index without a heap fetch.
            models.Index(
                models.F("created_at").asc(nulls_first=True),
                models.F("id"),
                "run_after",
                name="job_claim_queue_idx",
                condition=models.Q(status="pending", claimed_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"Job({self.id} kind={self.kind} status={self.status})"
