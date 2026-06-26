"""JobPost ↔ federation dispatch wiring.

Phase 5d of Plans/ActivityPub Phase 5 — federation proper. Each public
JobPost save/delete enqueues a federation fanout:

- ``post_save`` with ``created=True`` AND public → Create
- ``post_save`` after a private→public transition → Create
- ``post_save`` after a public→public transition on a content-bearing
  field → Update
- ``post_delete`` of a was-public JobPost → Delete (with the
  pre-delete audience captured by ``pre_delete``)

Updates are gated on a small ``FEDERATION_UPDATEABLE_FIELDS`` whitelist:
purely-administrative writes (e.g. ``last_seen_at`` bumps, ``apply_url``
resolution) don't fan out — every Update activity is a peer notification,
and we don't want to drown follower timelines with administrative noise.

Audience-transition detection is implemented via a ``__init__`` snapshot
of the row's audience value, refreshed on each ``post_save`` so the next
save sees the new baseline. The snapshot is per-instance, so two
threads/processes acting on the same row use their own snapshots — same
as Django's stock ``__init__``-snapshot idiom for dirty-checking.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from job_hunting.lib.as_object import AS2_PUBLIC, user_opted_into_rich
from job_hunting.lib.federation_dispatch import (
    enqueue_jobpost_activity,
    enqueue_jobpost_activity_for_company,
)
from job_hunting.models.job_application import JobApplication
from job_hunting.models.job_application_status import JobApplicationStatus
from job_hunting.models.job_post import JobPost
from job_hunting.models.score import Score


log = logging.getLogger(__name__)


# Content-bearing fields — a change to any of these warrants an Update
# activity to peers. Anything else (last_seen_at, apply_url_resolved_at,
# scrape_source admin fields, etc.) stays private to the local DB.
# Deliberately small — adding a field here means every Mastodon peer
# sees a notification on every change to it. Conservative is correct.
FEDERATION_UPDATEABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "link",
        "canonical_link",
        "company_id",
    }
)


_AUDIENCE_ATTR = "_federation_audience_snapshot"
_FIELDS_ATTR = "_federation_fields_snapshot"


def _is_public(audience) -> bool:
    if not isinstance(audience, list):
        return False
    return AS2_PUBLIC in audience


def _snapshot_jobpost(instance: JobPost) -> None:
    """Stash audience + content-bearing field values on the instance.

    Read by the post_save handler to detect public-transition and
    field-change Updates. Refreshed at the END of every post_save so
    the next save sees the new baseline (otherwise the second save
    would mis-detect the change as fresh).
    """
    audience = instance.audience if isinstance(instance.audience, list) else []
    setattr(instance, _AUDIENCE_ATTR, list(audience))
    setattr(
        instance,
        _FIELDS_ATTR,
        {field: getattr(instance, field, None) for field in FEDERATION_UPDATEABLE_FIELDS},
    )


@receiver(post_save, sender=JobPost)
def fanout_jobpost_save(sender, instance, created, **kwargs):
    """Enqueue Create on first public save, Update on content edits.

    Cases:

    - New row, public → Create.
    - New row, private → nothing.
    - Existing row, was private + now public → Create (first federation
      surface of this row).
    - Existing row, was public + now public + content-bearing field
      changed → Update.
    - Existing row, was public + now public + only administrative
      change → nothing.
    - Existing row, public → private → nothing (no Withdraw activity in
      V1; AS2 has no clean equivalent and Mastodon ignores it).

    The pre-save snapshot is read via ``getattr(instance, _AUDIENCE_ATTR, ...)``
    rather than refreshing from the DB so the signal doesn't re-trigger
    a query in the middle of a save. First save of an instance never
    has the attr set; we treat that as "was private" (no prior public
    state to retain).
    """
    prior_audience = getattr(instance, _AUDIENCE_ATTR, None)
    prior_fields = getattr(instance, _FIELDS_ATTR, None)
    is_public = _is_public(instance.audience)
    was_public = _is_public(prior_audience) if prior_audience is not None else False

    try:
        if created:
            if is_public:
                _enqueue(instance.pk, "create")
                _enqueue_company(instance, "create")
        elif not was_public and is_public:
            _enqueue(instance.pk, "create")
            _enqueue_company(instance, "create")
        elif was_public and is_public:
            if prior_fields is None or _content_changed(instance, prior_fields):
                _enqueue(instance.pk, "update", edit_marker=timezone.now())
    finally:
        # Refresh snapshot so the NEXT save off this instance sees the
        # current values as its baseline.
        _snapshot_jobpost(instance)


@receiver(pre_delete, sender=JobPost)
def snapshot_jobpost_for_delete(sender, instance, **kwargs):
    """Stash audience for the post_delete fanout decision.

    pre_delete fires before the row is gone, so we know the audience at
    delete-time. The post_delete handler reads this snapshot.
    """
    _snapshot_jobpost(instance)


@receiver(post_delete, sender=JobPost)
def fanout_jobpost_delete(sender, instance, **kwargs):
    """Enqueue Delete on removal of a was-public JobPost."""
    audience = getattr(instance, _AUDIENCE_ATTR, None)
    if audience is None:
        # No pre_delete snapshot (rare — direct ORM cascade) → fall back
        # to the instance's audience as-rendered. Still valid for the
        # public-check.
        audience = instance.audience
    if not _is_public(audience):
        return
    # The JobPost row is gone by the time we're called, so the dispatch
    # helper has to build the Delete activity from the in-memory
    # instance. enqueue_jobpost_activity loads from the DB, which won't
    # work here — so we'd lose the Delete dispatch. Sidestep by passing
    # the instance directly via a lower-level path: re-fetch + handle
    # the missing-row case as "build from the in-memory copy."
    #
    # Implementation: we synthesize a temporary save of the in-memory
    # instance into the dispatcher by leaning on its ``activity_kind ==
    # 'delete'`` branch which deliberately skips the public-gate (so the
    # caller can pass a row whose row-state may be inconsistent with the
    # instance). Same idea as `pre_delete` snapshotting: the trustworthy
    # state is the in-memory instance, not the (now-gone) DB row.
    _enqueue_delete_for_instance(instance)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _content_changed(instance: JobPost, prior: dict) -> bool:
    for field in FEDERATION_UPDATEABLE_FIELDS:
        if getattr(instance, field, None) != prior.get(field):
            return True
    return False


def _enqueue(jobpost_id: int, kind: str, **kwargs) -> None:
    try:
        enqueue_jobpost_activity(jobpost_id, kind, **kwargs)
    except Exception:
        # Never let federation fanout abort the JobPost save. We log and
        # move on; the row's still there, the operator can manually
        # re-enqueue from the dispatch_status command if needed.
        log.exception(
            "ap.signal.enqueue_failed jobpost_id=%s kind=%s", jobpost_id, kind
        )


def _enqueue_company(instance: JobPost, kind: str) -> None:
    """Phase 6a — fan out to the Company actor's followers when opted in.

    Cheap pre-gate on ``federation_enabled`` so the lib-layer DB hit is
    skipped for the >99% of saves that don't federate via the Company.
    The lib helper re-checks every gate (operator switch, audience,
    slug presence, follower count) so this fast-path miss has no
    correctness impact.
    """
    company_id = getattr(instance, "company_id", None)
    if company_id is None:
        return
    company = getattr(instance, "company", None)
    if company is None or not getattr(company, "federation_enabled", False):
        return
    try:
        enqueue_jobpost_activity_for_company(instance.pk, kind)
    except Exception:
        log.exception(
            "ap.signal.company_enqueue_failed jobpost_id=%s kind=%s",
            instance.pk,
            kind,
        )


def _enqueue_delete_for_instance(instance: JobPost) -> None:
    """Build and persist a Delete fanout from the in-memory JobPost.

    Bypasses enqueue_jobpost_activity's DB lookup since the row was just
    deleted. Otherwise mirrors its fanout loop: one
    ``FederationActivity`` row per unique inbox among accepted followers
    of the deleted post's owner.
    """
    from django.db import IntegrityError, transaction

    from job_hunting.lib.as_object import build_delete_activity_for_jobpost
    from job_hunting.lib.federation_dispatch import (
        _active_followers_for,
        _dedupe_inboxes,
        _schedule_dispatch_task,
    )
    from job_hunting.models import Actor, FederationActivity
    from job_hunting.models.federation_activity import (
        ACTIVITY_TYPE_DELETE,
        DELIVERY_PENDING,
        DIRECTION_OUTBOUND,
    )
    import json

    from django.conf import settings as dj_settings

    if not getattr(dj_settings, "ACTIVITYPUB_FEDERATION_ENABLED", True):
        return
    if instance.created_by_id is None:
        return
    actor = (
        Actor.objects.filter(user_id=instance.created_by_id, type="Person")
        .order_by("pk")
        .first()
    )
    if actor is None:
        return

    activity = build_delete_activity_for_jobpost(instance, actor)
    actor_uri_str = activity["actor"]
    body_json = json.dumps(activity)

    followers = _active_followers_for(instance.created_by_id)
    targets = _dedupe_inboxes(followers)
    if not targets:
        return

    now = timezone.now()
    for target_uri in targets:
        try:
            with transaction.atomic():
                row = FederationActivity.objects.create(
                    direction=DIRECTION_OUTBOUND,
                    activity_type=ACTIVITY_TYPE_DELETE,
                    activity_id=activity["id"],
                    actor_uri=actor_uri_str,
                    target_uri=target_uri,
                    local_user_id=instance.created_by_id,
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
        _schedule_dispatch_task(row.id, when=row.next_attempt_at)


# ---------------------------------------------------------------------------
# Snapshot baseline on row hydration
# ---------------------------------------------------------------------------
#
# The post_save handler reads the audience snapshot to detect transitions.
# For rows hydrated from the DB (queryset.get / .filter), we need the
# snapshot to reflect the DB state, not the in-memory mutation. Django's
# stock idiom is a ``__init__`` override — connect to ``post_init`` so
# we don't have to monkey-patch the model class.
from django.db.models.signals import post_init  # noqa: E402


@receiver(post_init, sender=JobPost)
def snapshot_on_load(sender, instance, **kwargs):
    """Capture audience baseline whenever a JobPost is instantiated."""
    _snapshot_jobpost(instance)


# ---------------------------------------------------------------------------
# BACK-99 (Task C) — fire an Update when the RICH data changes.
#
# Publishing happens at INGEST (`Profile.federate_posts` → audience=Public
# at create), so the first Create(Note) is always thin — verdict / score /
# applied don't exist yet. The JobPost-field `_content_changed` gate never
# fires for those (they live on Score / JobApplication / JobApplicationStatus,
# not on JobPost), so without this the rich body would never reach the
# fediverse. We re-emit an Update when the OWNER's own triage / score /
# application changes on a PUBLIC, RICH-opted-in post.
#
# Gates (all must hold, else no emit):
#   - the changed record belongs to the post's OWNER (created_by) — a
#     different user's score on the same shared post doesn't change what
#     the owner-attributed Note renders;
#   - the post is PUBLIC (audience contains AS2_PUBLIC);
#   - the owner opted into the RICH format — lean Notes don't carry this
#     data, so there is nothing to refresh.


def _maybe_enqueue_personal_update(job_post_id, actor_user_id) -> None:
    """Enqueue an Update iff an owner's rich annotation changed on a public,
    rich-opted-in post. Cheap-gates before the enqueue so the >99% of
    score/application writes that aren't public-rich-owner skip the work."""
    if not job_post_id or not actor_user_id:
        return
    # Load the FULL row — a ``.only()`` projection would defer the
    # FEDERATION_UPDATEABLE_FIELDS that `snapshot_on_load` (post_init)
    # reads, and accessing a deferred field there cascades
    # refresh_from_db → re-instantiate → post_init forever.
    jp = JobPost.objects.filter(pk=job_post_id).first()
    if jp is None:
        return
    # Non-owner change → the owner-attributed Note is unaffected → no emit.
    if jp.created_by_id != actor_user_id:
        return
    if not _is_public(jp.audience):
        return
    # Lean posts don't carry verdict/score/applied → nothing to refresh.
    if not user_opted_into_rich(actor_user_id):
        return
    _enqueue(job_post_id, "update", edit_marker=timezone.now())


@receiver(post_save, sender=Score)
def fanout_on_score_change(sender, instance, **kwargs):
    """A score write may change the rendered ``Strong match (N)`` segment."""
    _maybe_enqueue_personal_update(instance.job_post_id, instance.user_id)


@receiver(post_save, sender=JobApplication)
def fanout_on_application_change(sender, instance, **kwargs):
    """An application write only changes the rendered Note when it flips the
    ``applied`` segment — gate on ``applied_at`` so the empty triage-created
    row (applied_at NULL) doesn't spuriously re-emit."""
    if instance.applied_at is None:
        return
    _maybe_enqueue_personal_update(instance.job_post_id, instance.user_id)


@receiver(post_save, sender=JobApplicationStatus)
def fanout_on_application_status_change(sender, instance, **kwargs):
    """A triage status write may change the rendered verdict segment.

    The owner + post are resolved off the parent JobApplication (one
    lookup); the per-post gates in ``_maybe_enqueue_personal_update`` do
    the rest."""
    app = (
        JobApplication.objects.filter(pk=instance.application_id)
        .values("job_post_id", "user_id")
        .first()
    )
    if not app:
        return
    _maybe_enqueue_personal_update(app["job_post_id"], app["user_id"])
