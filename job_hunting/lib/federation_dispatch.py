"""ActivityPub Phase 5d — outbound dispatch worker.

Fan out a JobPost create / update / delete event to every accepted
``FederationFollower`` of the post's author. Two pieces:

1. ``enqueue_jobpost_activity(jobpost_id, kind)`` — synchronous
   entry-point called from the JobPost signals. Loads the JobPost,
   gates on the AS2-Public audience, materializes one
   ``FederationActivity`` row per unique target inbox (sharedInbox
   deduped where present), and schedules a ``dispatch_one`` task
   per row via django-q2 ``async_task``.

2. ``dispatch_one(federation_activity_id)`` — qcluster worker entry
   point. Signs the persisted activity body with the local actor's
   private key and POSTs it to the row's ``target_uri``. Outcomes:

   - 2xx → ``delivered``
   - 4xx → ``rejected`` (peer told us no; don't retry)
   - 5xx / timeout / network error → schedule a retry per the backoff
     schedule, increment ``retry_count``; dead-letter on the 6th attempt.

3. ``sweep_pending_dispatches()`` — periodic belt-and-suspenders that
   picks up stuck rows (worker was down when their ``next_attempt_at``
   came due) and re-enqueues them. Registered as a django-q2 Schedule
   by migration 0090.

The "synchronous helper, async worker" split keeps the request-handler
path tight (the signal fires from inside a JobPost.save / .delete —
expensive fanout would block the response) while still letting tests
run synchronously via ``Q_CLUSTER["sync"] = True``.

Operator kill-switch: ``ACTIVITYPUB_FEDERATION_ENABLED = False`` in
settings short-circuits ``enqueue_jobpost_activity`` — no rows
materialized, no tasks scheduled. Worker entry points stay live so any
already-enqueued in-flight tasks can drain cleanly.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from job_hunting.lib import federation_signing
from job_hunting.lib.as_object import (
    AS2_PUBLIC,
    build_create_activity_for_jobpost,
    build_delete_activity_for_jobpost,
    build_update_activity_for_jobpost,
)
from job_hunting.models import (
    Actor,
    FederationActivity,
    FederationFollower,
    JobPost,
)
from job_hunting.models.federation_activity import (
    ACTIVITY_TYPE_CREATE,
    ACTIVITY_TYPE_DELETE,
    ACTIVITY_TYPE_UPDATE,
    DELIVERY_DEAD_LETTER,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_REJECTED,
    DIRECTION_OUTBOUND,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue_jobpost_activity(
    jobpost_id: int,
    activity_kind: str,
    *,
    edit_marker=None,
) -> int:
    """Materialize FederationActivity rows + schedule dispatch tasks.

    Returns the number of rows enqueued (== unique inbox URIs across the
    follower set after sharedInbox dedupe). Returns 0 when:

    - federation is operator-disabled (``ACTIVITYPUB_FEDERATION_ENABLED=False``)
    - the JobPost is not public (no ``AS2_PUBLIC`` in its audience)
    - the JobPost is a Delete and was not public — caller responsibility
      to remember the pre-delete audience; the signals handler stashes it
    - no Actor exists for the JobPost's ``created_by`` user
    - no accepted FederationFollower rows exist for the user
    - every candidate FederationActivity row already exists (idempotent
      re-enqueue is a no-op rather than an error)

    ``edit_marker`` is forwarded to the Update activity builder when
    ``activity_kind == "update"`` so each edit produces a distinct
    activity id. Callers (signals) typically pass ``timezone.now()``.
    """
    if not getattr(settings, "ACTIVITYPUB_FEDERATION_ENABLED", True):
        log.debug("ap.dispatch.disabled jobpost_id=%s kind=%s", jobpost_id, activity_kind)
        return 0

    jp = JobPost.objects.filter(pk=jobpost_id).select_related("created_by").first()
    if jp is None:
        log.warning("ap.dispatch.missing_jobpost jobpost_id=%s", jobpost_id)
        return 0

    if activity_kind != "delete" and not _is_public(jp):
        log.debug("ap.dispatch.skip_private jobpost_id=%s kind=%s", jobpost_id, activity_kind)
        return 0

    if jp.created_by_id is None:
        return 0

    actor = (
        Actor.objects.filter(user_id=jp.created_by_id, type="Person")
        .order_by("pk")
        .first()
    )
    if actor is None:
        log.info(
            "ap.dispatch.no_actor jobpost_id=%s user_id=%s — skipping fanout",
            jobpost_id,
            jp.created_by_id,
        )
        return 0

    activity = _build_activity(jp, actor, activity_kind, edit_marker=edit_marker)
    activity_type_value = _activity_type_value(activity_kind)
    actor_uri_str = activity["actor"]
    body_json = json.dumps(activity)

    followers = _active_followers_for(jp.created_by_id)
    targets = _dedupe_inboxes(followers)
    if not targets:
        log.info(
            "ap.dispatch.no_followers jobpost_id=%s user_id=%s kind=%s",
            jobpost_id,
            jp.created_by_id,
            activity_kind,
        )
        return 0

    now = timezone.now()
    enqueued = 0
    log.info(
        "ap.dispatch.fanout jobpost_id=%s kind=%s targets=%s",
        jobpost_id,
        activity_kind,
        len(targets),
    )
    for target_uri in targets:
        # The (direction, activity_id) unique constraint is the
        # deduplication line of defence. Re-enqueueing the same Create
        # (e.g. signal fires twice across a transaction retry) silently
        # drops onto the existing row rather than ballooning the audit
        # log. Update + Delete have id discriminators that prevent that
        # collision unless the caller passes the exact same edit_marker.
        try:
            with transaction.atomic():
                row = FederationActivity.objects.create(
                    direction=DIRECTION_OUTBOUND,
                    activity_type=activity_type_value,
                    activity_id=activity["id"],
                    actor_uri=actor_uri_str,
                    target_uri=target_uri,
                    local_user_id=jp.created_by_id,
                    body=body_json,
                    delivery_status=DELIVERY_PENDING,
                    next_attempt_at=now,
                )
        except IntegrityError:
            # An existing row covers this (direction, activity_id) —
            # but the prior row may be for a different target URI
            # (multiple followers on the same server we forgot to dedupe
            # at the time). Fall back to per-target lookup so the unique
            # constraint stays useful without losing a fanout target.
            row = FederationActivity.objects.filter(
                direction=DIRECTION_OUTBOUND,
                activity_id=activity["id"],
                target_uri=target_uri,
            ).first()
            if row is None:
                # The unique violation was on (direction, activity_id)
                # with a different target. Skip — the caller's prior
                # enqueue covered the contract for this activity.
                log.debug(
                    "ap.dispatch.idempotent_skip activity_id=%s target=%s",
                    activity["id"],
                    target_uri,
                )
                continue
        enqueued += 1
        _schedule_dispatch_task(row.id, when=row.next_attempt_at)

    return enqueued


def dispatch_one(federation_activity_id: int) -> None:
    """Sign + POST a persisted outbound activity to its target inbox.

    Outcome map:

    - 2xx → ``delivered`` (terminal).
    - 4xx → ``rejected`` (terminal). Peer told us no — retry is futile.
    - 5xx / network error / timeout (``status_code == 0`` from the
      signing module's ``deliver``) → schedule retry per the backoff
      schedule. After ``ACTIVITYPUB_DISPATCH_DEAD_LETTER_AT_RETRY``
      attempts, dead-letter (terminal).

    Re-entrant safe: gates on ``delivery_status == 'pending'`` so a
    duplicate task (e.g. the periodic sweep re-scheduled while the
    primary task was already in-flight) becomes a no-op.
    """
    row = FederationActivity.objects.filter(pk=federation_activity_id).first()
    if row is None:
        log.warning("ap.dispatch_one.missing row_id=%s", federation_activity_id)
        return
    if row.direction != DIRECTION_OUTBOUND:
        log.warning(
            "ap.dispatch_one.wrong_direction row_id=%s direction=%s",
            federation_activity_id,
            row.direction,
        )
        return
    if row.delivery_status != DELIVERY_PENDING:
        log.debug(
            "ap.dispatch_one.terminal row_id=%s status=%s — skipping",
            federation_activity_id,
            row.delivery_status,
        )
        return

    actor = _local_actor_for(row)
    if actor is None:
        # Local user removed between enqueue and dispatch — there's no
        # private key to sign with. Mark failed terminal so the row
        # doesn't ping back forever; future investigation lands here.
        row.delivery_status = DELIVERY_FAILED
        row.delivery_error = "no local actor to sign with"
        row.next_attempt_at = None
        row.save(update_fields=["delivery_status", "delivery_error", "next_attempt_at"])
        log.warning("ap.dispatch_one.no_actor row_id=%s", federation_activity_id)
        return

    body_bytes = row.body.encode("utf-8")
    target_host = FederationFollower.host_for_uri(row.target_uri or "")
    attempt_n = row.retry_count + 1

    log.info(
        "ap.dispatch_one.start row_id=%s host=%s attempt=%s",
        federation_activity_id,
        target_host,
        attempt_n,
    )

    try:
        status_code, snippet = federation_signing.deliver(
            row.target_uri,
            body_bytes,
            actor,
        )
    except Exception as exc:  # pragma: no cover - signing module shouldn't raise, but
        log.exception("ap.dispatch_one.unexpected row_id=%s", federation_activity_id)
        _record_transient_failure(
            row, status_code=0, snippet=f"{type(exc).__name__}: {exc}"
        )
        return

    log.info(
        "ap.dispatch_one.result row_id=%s host=%s attempt=%s status=%s",
        federation_activity_id,
        target_host,
        attempt_n,
        status_code,
    )

    if 200 <= status_code < 300:
        row.delivery_status = DELIVERY_DELIVERED
        row.delivered_at = timezone.now()
        row.delivery_error = None
        row.next_attempt_at = None
        row.retry_count = attempt_n
        row.save(
            update_fields=[
                "delivery_status",
                "delivered_at",
                "delivery_error",
                "next_attempt_at",
                "retry_count",
            ]
        )
        return
    if 400 <= status_code < 500:
        row.delivery_status = DELIVERY_REJECTED
        row.delivery_error = _format_error(status_code, snippet)
        row.next_attempt_at = None
        row.retry_count = attempt_n
        row.save(
            update_fields=[
                "delivery_status",
                "delivery_error",
                "next_attempt_at",
                "retry_count",
            ]
        )
        return

    _record_transient_failure(row, status_code=status_code, snippet=snippet)


def sweep_pending_dispatches() -> int:
    """Belt-and-suspenders: pick up rows whose next_attempt_at is overdue.

    Runs every minute via the Schedule registered in migration 0090.
    Re-enqueues a ``dispatch_one`` task for each row in
    ``delivery_status='pending' AND next_attempt_at <= now()``.

    Idempotent — ``dispatch_one`` re-gates on ``delivery_status``, so a
    spurious double-enqueue collapses to one delivery attempt.

    Returns the count of rows re-enqueued (also surfaces nicely in
    qcluster logs).
    """
    now = timezone.now()
    overdue = FederationActivity.objects.filter(
        direction=DIRECTION_OUTBOUND,
        delivery_status=DELIVERY_PENDING,
        next_attempt_at__lte=now,
    ).values_list("pk", flat=True)
    pks = list(overdue)
    if pks:
        log.info("ap.dispatch.sweep recovered=%s", len(pks))
    for pk in pks:
        _schedule_dispatch_task(pk)
    return len(pks)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_public(jp: JobPost) -> bool:
    audience = jp.audience
    if not isinstance(audience, list):
        return False
    return AS2_PUBLIC in audience


def _build_activity(jp: JobPost, actor: Actor, kind: str, *, edit_marker=None) -> dict:
    if kind == "create":
        return build_create_activity_for_jobpost(jp, actor)
    if kind == "update":
        return build_update_activity_for_jobpost(jp, actor, edit_marker=edit_marker)
    if kind == "delete":
        return build_delete_activity_for_jobpost(jp, actor)
    raise ValueError(f"unknown activity_kind: {kind!r}")


def _activity_type_value(kind: str) -> str:
    return {
        "create": ACTIVITY_TYPE_CREATE,
        "update": ACTIVITY_TYPE_UPDATE,
        "delete": ACTIVITY_TYPE_DELETE,
    }[kind]


def _active_followers_for(user_id: int) -> Iterable[FederationFollower]:
    return list(
        FederationFollower.objects.filter(
            local_user_id=user_id,
            accepted_at__isnull=False,
            unfollowed_at__isnull=True,
        )
    )


def _dedupe_inboxes(followers: Iterable[FederationFollower]) -> list[str]:
    """Collapse follower list to unique inbox URIs.

    Mastodon documents the sharedInbox optimisation: when present,
    delivering one POST to the sharedInbox URL covers every follower on
    that instance. We mirror that — group followers by
    ``COALESCE(shared_inbox_uri, inbox_uri)`` and emit each unique target
    once. Ordering is insertion-stable so the resulting fanout row order
    is reproducible (helpful for tests).
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for follower in followers:
        target = follower.shared_inbox_uri or follower.inbox_uri
        if not target:
            continue
        if target in seen_set:
            continue
        seen_set.add(target)
        seen.append(target)
    return seen


def _local_actor_for(row: FederationActivity) -> Actor | None:
    return (
        Actor.objects.filter(user_id=row.local_user_id, type="Person")
        .order_by("pk")
        .first()
    )


def _record_transient_failure(
    row: FederationActivity,
    *,
    status_code: int,
    snippet: str,
) -> None:
    """Bump retry_count + schedule next attempt, or dead-letter on exhaustion."""
    backoff = getattr(
        settings,
        "ACTIVITYPUB_DISPATCH_RETRY_BACKOFF_SECONDS",
        [60, 300, 1800, 14400, 86400],
    )
    dead_letter_at = getattr(
        settings, "ACTIVITYPUB_DISPATCH_DEAD_LETTER_AT_RETRY", 6
    )

    new_retry_count = row.retry_count + 1
    error = _format_error(status_code, snippet)
    log.info(
        "ap.dispatch_one.transient row_id=%s attempt=%s status=%s",
        row.pk,
        new_retry_count,
        status_code,
    )
    if new_retry_count >= dead_letter_at:
        row.delivery_status = DELIVERY_DEAD_LETTER
        row.delivery_error = "exceeded retry budget"
        row.retry_count = new_retry_count
        row.next_attempt_at = None
        row.save(
            update_fields=[
                "delivery_status",
                "delivery_error",
                "retry_count",
                "next_attempt_at",
            ]
        )
        log.warning(
            "ap.dispatch_one.dead_letter row_id=%s retry_count=%s last_error=%s",
            row.pk,
            new_retry_count,
            error,
        )
        return

    # backoff[i] is the wait before attempt (i+2); retry_count just
    # became (i+1), so index in backoff is retry_count - 1.
    idx = min(new_retry_count - 1, len(backoff) - 1)
    delay = backoff[idx]
    next_attempt = timezone.now() + timedelta(seconds=delay)
    row.retry_count = new_retry_count
    row.delivery_error = error
    row.next_attempt_at = next_attempt
    row.save(
        update_fields=[
            "retry_count",
            "delivery_error",
            "next_attempt_at",
        ]
    )
    _schedule_dispatch_task(row.pk, when=next_attempt)


def _format_error(status_code: int, snippet: str) -> str:
    return f"status={status_code} body={snippet[:480]}"


def _schedule_dispatch_task(federation_activity_id: int, *, when=None) -> None:
    """Enqueue ``dispatch_one`` via django-q2.

    Routing:

    - ``when`` omitted (or already past) → fire immediately via
      ``async_task`` so the qcluster process picks it up on its next
      poll.
    - ``when`` is a future datetime → create a one-shot ``Schedule`` row
      (``schedule_type='O'``, ``next_run=when``, ``repeats=-1``) — that's
      how django-q2 1.10 expresses "run this at time T." The qcluster
      converts ``Schedule.ONCE`` rows into tasks at next-run time, then
      sets ``repeats=0`` so the row doesn't fire again.

    Imports are lazy so the queue's models don't drag into test
    environments that don't need them.
    """
    from django_q.tasks import async_task

    func_path = "job_hunting.lib.federation_dispatch.dispatch_one"
    if when is None or when <= timezone.now():
        async_task(func_path, federation_activity_id=federation_activity_id)
        return

    # Future-dated → one-shot Schedule. django-q2 stores args as a
    # repr-style tuple; kwargs as a python-dict repr string. Encoding
    # mirrors the qcluster's own parser (see django_q.tasks.schedule).
    from django_q.models import Schedule

    Schedule.objects.create(
        name=f"ap-dispatch-{federation_activity_id}-{int(when.timestamp())}",
        func=func_path,
        kwargs=f"federation_activity_id={federation_activity_id}",
        schedule_type=Schedule.ONCE,
        next_run=when,
        repeats=-1,
    )
