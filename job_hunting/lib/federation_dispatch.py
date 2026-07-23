"""ActivityPub Phase 5d — outbound dispatch worker.

Fan out a JobPost create / update / delete event to every accepted
``FederationFollower`` of the post's author. Two pieces:

1. ``enqueue_jobpost_activity(jobpost_id, kind)`` — synchronous
   entry-point called from the JobPost signals. Loads the JobPost,
   gates on the AS2-Public audience, materializes one
   ``FederationActivity`` row per unique target inbox (sharedInbox
   deduped where present), and schedules a ``dispatch_one`` task
   per row via the unified ``enqueue('federation_dispatch', ...)``
   producer (CC-206).

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


def enqueue_jobpost_activity_for_company(
    jobpost_id: int,
    activity_kind: str,
) -> int:
    """Materialize Create(Note) rows attributed to the JobPost's Company actor.

    Phase 6a. Fans out a public JobPost to the *Company*'s federation
    followers (separate set from the author User's followers). Gates:

    - federation operator-disabled → 0
    - JobPost not public → 0
    - JobPost has no Company → 0
    - Company has ``federation_enabled = False`` → 0
    - Company has no slug yet (backfill_company_slugs hasn't run) → 0
    - no Organization Actor exists for the Company AND we can't
      lazy-create one (no slug) → 0
    - no active followers on the Company actor → 0

    Only ``activity_kind == "create"`` is supported in Phase 6a; the
    Update / Delete shapes attributed to a Company actor land in 6e.
    Returns the number of rows materialized.
    """
    if activity_kind != "create":
        # Phase 6a scope guard — fail loud so a future 6e change has to
        # explicitly opt in rather than silently shipping nothing.
        log.debug(
            "ap.dispatch.company.skip_kind kind=%s — only 'create' is wired in 6a",
            activity_kind,
        )
        return 0
    if not getattr(settings, "ACTIVITYPUB_FEDERATION_ENABLED", True):
        return 0

    jp = (
        JobPost.objects.filter(pk=jobpost_id)
        .select_related("company")
        .first()
    )
    if jp is None or jp.company_id is None:
        return 0
    if not _is_public(jp):
        return 0
    company = jp.company
    if not getattr(company, "federation_enabled", False):
        return 0
    if not company.slug:
        log.info(
            "ap.dispatch.company.no_slug jobpost_id=%s company_id=%s — run backfill_company_slugs",
            jobpost_id,
            company.id,
        )
        return 0

    # Lazy-create the Company Organization actor so first-publish
    # doesn't require an out-of-band materialization step. Same
    # idempotent upsert the AS2 view path uses.
    actor = Actor.objects.filter(company_id=company.id).first()
    if actor is None:
        # Avoid pulling the view-layer helper here (would create a
        # circular import between views and lib). Inline the upsert
        # under the same lock semantics.
        from job_hunting.models.actor import ACTOR_TYPE_ORGANIZATION

        with transaction.atomic():
            actor = (
                Actor.objects.select_for_update()
                .filter(company_id=company.id)
                .first()
            )
            if actor is None:
                actor = Actor.objects.create(
                    company=company,
                    type=ACTOR_TYPE_ORGANIZATION,
                    preferred_username=company.slug,
                )

    activity = _build_company_create_activity(jp, company, actor)
    actor_uri_str = activity["actor"]
    body_json = json.dumps(activity)
    activity_type_value = ACTIVITY_TYPE_CREATE

    followers = _active_company_followers_for(company.id)
    targets = _dedupe_inboxes(followers)
    if not targets:
        log.info(
            "ap.dispatch.company.no_followers jobpost_id=%s company_id=%s",
            jobpost_id,
            company.id,
        )
        return 0

    now = timezone.now()
    enqueued = 0
    log.info(
        "ap.dispatch.company.fanout jobpost_id=%s company_id=%s targets=%s",
        jobpost_id,
        company.id,
        len(targets),
    )
    for target_uri in targets:
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
            row = FederationActivity.objects.filter(
                direction=DIRECTION_OUTBOUND,
                activity_id=activity["id"],
                target_uri=target_uri,
            ).first()
            if row is None:
                continue
        enqueued += 1
        _schedule_dispatch_task(row.id, when=row.next_attempt_at)

    return enqueued


def _build_company_create_activity(jp: JobPost, company, actor: Actor) -> dict:
    """Build a Create(Note) envelope attributed to a Company actor.

    Mirrors the view-layer helper of the same shape. Lifted here so
    the dispatch worker doesn't import from views/federation.py (which
    would introduce a circular dependency).
    """
    activity = build_create_activity_for_jobpost(jp, actor)
    origin = activity["actor"].rsplit("/actors/", 1)[0] if "/actors/" in activity["actor"] else ""
    if not origin:
        origin = activity["actor"].rsplit("/", 1)[0]
    # Rewrite Person-actor URI shape to Company-actor URI shape so the
    # activity envelope advertises the Organization page.
    company_actor_uri = activity["actor"].replace(
        f"/actors/{actor.preferred_username}",
        f"/companies/{company.slug}",
    )
    # When the actor URI didn't take the /actors/<u> form (defensive —
    # the Phase 5b helper always does), fall back to building from
    # scratch via the origin extraction above.
    if company_actor_uri == activity["actor"]:
        company_actor_uri = f"{origin}/companies/{company.slug}"
    activity["actor"] = company_actor_uri
    activity["cc"] = [f"{company_actor_uri}/followers"]
    inner = activity.get("object")
    if isinstance(inner, dict):
        inner["attributedTo"] = company_actor_uri
    return activity


def _active_company_followers_for(company_id: int):
    """Active followers of a Company actor.

    Phase 6a wires the dispatch shape but the follower model is
    keyed by ``local_user_id`` — Companies don't carry a user. We
    extend FederationFollower lookup to support ``company_id`` once
    the inbox follow-handler lands (separate ticket). For now the
    queryset is empty by definition: nobody can follow a Company
    yet, so the fanout returns 0 targets and the early-return above
    catches it.

    Once 6a-followers ships, this becomes a real lookup; the call
    site is already in place so wiring the second half is a one-file
    change.
    """
    # TODO(6a-followers): switch to FederationFollower.objects.filter(
    #     company_id=company_id, accepted_at__isnull=False, ...)
    # once the Company-follower migration lands.
    return list(FederationFollower.objects.none())


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
    """Enqueue ``dispatch_one`` via the unified async producer (CC-206).

    ``enqueue('federation_dispatch', ...)`` picks the transport by
    ``CC_TASKS_ENABLED`` (a Cloud Task → ``/tasks/run-job/`` on GCP, or a
    ``Job`` row drained by ``run_jobs`` on self-host) — the same seam the
    score/answer/JA-match paths use.

    Routing:

    - ``when`` omitted (or already past) → fire immediately.
    - ``when`` is a future datetime → pass ``run_after=when``. ``enqueue``
      maps that to the Cloud Tasks ``schedule_time`` (GCP) / ``Job.run_after``
      gate (self-host), the native delayed-dispatch primitive — replacing the
      old django-q2 one-shot ``Schedule`` row.

    The retry state machine is unchanged: ``dispatch_one`` re-gates on
    ``delivery_status`` and, on a transient failure, bumps
    ``retry_count``/``next_attempt_at`` and re-schedules via this fn; the
    ``sweep_pending_dispatches`` sweep (CC-213, now on the GCP Cloud Scheduler
    clock) re-drives any row whose ``next_attempt_at`` has passed. So delayed
    retry is fully covered on both transports even if a single scheduled
    dispatch is dropped.
    """
    from job_hunting.lib.cloud_tasks import enqueue

    run_after = when if (when is not None and when > timezone.now()) else None
    enqueue(
        "federation_dispatch",
        run_after=run_after,
        federation_activity_id=federation_activity_id,
    )
