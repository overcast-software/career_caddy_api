"""ActivityPub Phase 5e — federated JobPost ingestion + dedup.

Inbox-side counterpart to ``lib/federation_dispatch`` (5d): inbound
``Create(Note)`` activities verified by the 5c inbox handler arrive
here, get defensively parsed against ``IngestedNote``, walk the dedup
decision tree, and either:

  * CREATED — new JobPost row inserted with ``source="activitypub"``
    and ``source_instance`` set to the remote peer's host.
  * MERGED — existing local JobPost matched by canonical_link; a
    DuplicateAnnotation row with action=``federated_merge`` is written
    for the audit trail, the inbound JobPost is NOT created (we keep
    the local copy as the canonical surface), and the caller learns
    which local row absorbed the inbound activity via the returned
    tuple.
  * REJECTED — defensive-parse failure, sticky-closed override, AS2
    Public missing from audience (private content snuck through), or
    per-instance hourly quota exhausted. Inbound activity stays logged
    on FederationActivity; no JobPost touched.
  * SKIPPED — activity object isn't a Note/Article (e.g. Image, Page,
    Announce wrapper). Caller leaves the activity logged-only.

The four-clause dedup walk this implements:

  1. canonical_link normalization  →  ``canonicalize_link`` on Note.url
  2. canonical_link exact match    →  ``find_duplicate`` short-circuits
                                       on canonical_link before the
                                       fingerprint branch
  3. sticky-closed override        →  refuse to merge into a
                                       ``posting_status="closed"`` local
                                       row (closing is a deliberate
                                       user act; a remote refanout
                                       cannot reopen)
  4. response-shape visibility     →  enforced at the JobPostViewSet
                                       five-clause filter; federated
                                       rows ride in with ``created_by``
                                       NULL so they never match
                                       unless the requesting user also
                                       owns a JobPostDiscovery for them

Fingerprint (clause 2's sibling) is *intentionally skipped* for
federated ingestion: ``fingerprint()`` requires ``company_id``, and
federated rows arrive without a Company FK (Company is per-user
namespaced — see project memory ``company_shared``). Without
company_id, fingerprint() returns None and the fingerprint branch is
inert by definition. If a future AS2 ``careercaddy:extension`` payload
carries company metadata, that's the place to plumb a fingerprint
match in; until then canonical_link IS the dedup signal for the
federated path, and that's intentional.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from pydantic import BaseModel, ValidationError, field_validator

from job_hunting.models import (
    DuplicateAnnotation,
    FederationActivity,
    JobPost,
)
from job_hunting.models.job_post import AS2_PUBLIC
from job_hunting.models.job_post_dedupe import canonicalize_link, find_duplicate


logger = logging.getLogger(__name__)


# Ingest outcome strings. Returned verbatim to callers + written to
# FederationActivity.delivery_error on the rejected branch so the audit
# row's reason is human-readable.
OUTCOME_CREATED = "created"
OUTCOME_MERGED = "merged"
OUTCOME_REJECTED = "rejected"
OUTCOME_SKIPPED = "skipped"

# AS2 object types we accept as JobPost candidates. Note is the canonical
# Mastodon-style microblog object; Article is the long-form variant some
# instances (Plume, WriteFreely) use for posts that happen to be job
# listings. Anything else falls through to OUTCOME_SKIPPED so we don't
# turn Images / Audio / Pages into JobPosts.
INGEST_NOTE_TYPES = {"Note", "Article"}


class IngestedNote(BaseModel):
    """Defensive schema for an inbound AS2 Note/Article object.

    Pydantic enforces the field shape; downstream code never has to
    re-validate. Field aliases mirror the AS2 vocab — ``object.name``,
    ``object.content``, etc.

    ``model_config`` allows extras because peers routinely carry
    instance-specific extensions (Mastodon's ``sensitive``, Pleroma's
    ``directMessage``, custom ``careercaddy:*`` keys) we want to log but
    not validate. The ``content`` size cap is enforced in
    ``ingest_create_note`` against the settings-driven ceiling so a
    schema-validation hit during tests doesn't bypass the operator's
    configured limit.
    """

    type: str
    url: str
    content: str
    published: datetime
    name: Optional[str] = None

    model_config = {"extra": "allow"}

    @field_validator("url")
    @classmethod
    def _url_parseable(cls, value: str) -> str:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("url missing scheme or host")
        return value

    @field_validator("type")
    @classmethod
    def _type_supported(cls, value: str) -> str:
        # Validation here is permissive — both Note and Article are
        # accepted; the SKIPPED branch lives in ingest_create_note so
        # callers can distinguish "wrong type for us" from "schema
        # failure". Reject only the empty string (which would silently
        # default the dispatcher into truthiness traps).
        if not value:
            raise ValueError("type required")
        return value


@dataclass
class IngestResult:
    """Returned by ``ingest_create_note``. ``job_post`` is None for
    REJECTED + SKIPPED outcomes."""

    outcome: str
    job_post: Optional[JobPost] = None
    reason: Optional[str] = None


def _instance_host(activity: dict) -> str:
    """Resolve the peer instance host from an activity envelope.

    Walks ``attributedTo`` → ``actor`` so peers that put authorship on
    either field land on the same bucket. Falls back to empty string if
    the activity is malformed; the caller treats empty-string host as
    "no host" and ingest_create_note's host-required guard takes over.
    """
    candidate = None
    obj = activity.get("object") if isinstance(activity, dict) else None
    if isinstance(obj, dict):
        candidate = obj.get("attributedTo") or obj.get("actor")
    if not candidate:
        candidate = activity.get("actor") if isinstance(activity, dict) else None
    if not isinstance(candidate, str) or not candidate:
        return ""
    return urlparse(candidate).netloc.lower()


def _audience_for(activity: dict) -> list:
    """Return the activity's audience field as a normalized list.

    AS2 carries audience in ``to``/``cc`` on the activity envelope AND
    inside the object. We union all of them so a peer that only marks
    the envelope as Public (Mastodon's default for public posts) still
    lands in our public bucket. Returns ``[]`` if nothing carries the
    AS2 Public URI — caller treats that as a private/follower-only post
    we have no business storing.
    """
    seen: list = []
    obj = activity.get("object") if isinstance(activity, dict) else None
    sources = []
    if isinstance(activity, dict):
        sources.extend([activity.get("to"), activity.get("cc")])
    if isinstance(obj, dict):
        sources.extend([obj.get("to"), obj.get("cc"), obj.get("audience")])
    for src in sources:
        if isinstance(src, str):
            seen.append(src)
        elif isinstance(src, list):
            seen.extend(s for s in src if isinstance(s, str))
    return seen


def _quota_check_and_increment(instance_host: str) -> bool:
    """Per-peer hourly quota gate. Returns True if increment succeeded
    (room remaining), False if the bucket is full.

    Only ``created`` outcomes call this — ``merged`` skips the gate by
    design so a fan-out of duplicates doesn't lock a peer out of
    legitimate new posts (see module docstring).

    The bucket key collapses to the wall-clock hour so the window rolls
    naturally; this is coarser than a true sliding window but plenty
    for the fediverse rate the contract assumes.
    """
    if not instance_host:
        # Empty host means we couldn't attribute the activity; the gate
        # only protects us from per-peer floods, not unattributed
        # activities (those die at signature verification upstream).
        return True
    limit = getattr(settings, "ACTIVITYPUB_INGEST_INSTANCE_QUOTA_PER_HOUR", 100)
    hour = int(timezone.now().timestamp() // 3600)
    key = f"ap:ingest_quota:{instance_host}:{hour}"
    try:
        current = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3700)
        current = 1
    return current <= limit


def _mark_rejected(
    federation_activity: Optional[FederationActivity],
    reason: str,
) -> IngestResult:
    """Persist the reject reason on the 5c audit row + return result.

    Writes both ``delivery_status`` (``rejected``) and ``delivery_error``
    (the human-readable reason). 5c initially landed the row with
    ``delivery_status=accepted`` (signature passed); 5e demoting it to
    ``rejected`` carries the semantic that the activity verified BUT
    failed our ingest-time content checks.
    """
    if federation_activity is not None:
        try:
            FederationActivity.objects.filter(pk=federation_activity.pk).update(
                delivery_status="rejected",
                delivery_error=f"ingest_rejected: {reason}",
            )
        except Exception:  # pragma: no cover - audit write is best-effort
            logger.exception("ap.5e.audit_update_failed pk=%s", federation_activity.pk)
    return IngestResult(outcome=OUTCOME_REJECTED, reason=reason)


def _write_federated_merge_annotation(
    candidate_canonical_link: str,
    merged_to: JobPost,
    activity: dict,
) -> None:
    """Audit a federated merge. Writes a DuplicateAnnotation row pointing
    from the would-have-been-created candidate (we never persisted it,
    so ``from_jp`` is the merge target itself — annotations are
    SET_NULL on JobPost delete and ``from_jp`` is non-null, so we
    can't leave it blank).

    The ``signal_state`` captures the remote provenance: the activity
    id, the remote actor URI, and the canonical_link we matched on.
    The dedupe-feedback report can later filter on
    ``action=federated_merge`` and inspect ``signal_state`` to ask
    "which peers contributed the most merge-traffic" — that's the
    audit-trail value of writing this row instead of dropping the
    activity silently.
    """
    obj = activity.get("object") if isinstance(activity, dict) else {}
    obj_dict: dict = obj if isinstance(obj, dict) else {}
    signal_state = {
        "federation": {
            "activity_id": activity.get("id") if isinstance(activity, dict) else None,
            "actor_uri": activity.get("actor") if isinstance(activity, dict) else None,
            "object_id": obj_dict.get("id"),
            "canonical_link": candidate_canonical_link,
        }
    }
    DuplicateAnnotation.objects.create(
        from_jp=merged_to,
        to_jp=merged_to,
        previous_to=None,
        action=DuplicateAnnotation.FEDERATED_MERGE,
        set_by=None,
        signal_state=signal_state,
    )


def ingest_create_note(
    activity: dict,
    federation_activity: Optional[FederationActivity] = None,
) -> IngestResult:
    """Walk an inbound Create(Note) through the dedup decision tree.

    Entry point used by ``api/views/federation.actor_inbox``'s
    Create(Note) branch AFTER signature verification + activity
    logging. Operates against the in-memory activity dict (not the
    persisted body) so the caller can pass a freshly-parsed dict
    without round-tripping JSON.

    Returns an :class:`IngestResult`; callers consult ``.outcome`` to
    drive the 5c audit row's terminal ``delivery_status`` (accepted
    vs. rejected) and metrics.
    """
    if not getattr(settings, "ACTIVITYPUB_INGEST_ENABLED", True):
        # Operator kill-switch: the inbox handler should not even call us
        # in this state, but be defensive — a stray replay walk should
        # also no-op when ingest is disabled.
        return IngestResult(outcome=OUTCOME_SKIPPED, reason="ingest_disabled")

    obj = activity.get("object") if isinstance(activity, dict) else None
    if not isinstance(obj, dict):
        return _mark_rejected(federation_activity, "object_not_object")

    obj_type = obj.get("type")
    if obj_type not in INGEST_NOTE_TYPES:
        # Image / Page / Announce wrapper / etc — leave logged, do
        # nothing else. NOT a rejection: the peer is well-formed, the
        # activity just isn't a JobPost candidate. Audit row keeps its
        # 5c ``accepted`` delivery_status.
        return IngestResult(outcome=OUTCOME_SKIPPED, reason=f"object_type={obj_type!r}")

    # Audience guard — federated content MUST carry AS2 Public to be
    # ingested. Without it the peer is pushing private / follower-only
    # content into our inbox, which is either a bug on their side or a
    # malicious spoof. Reject. (5d only fans out public posts on our
    # side; the symmetric rule on ingest is the contract.)
    audience = _audience_for(activity)
    if AS2_PUBLIC not in audience:
        return _mark_rejected(federation_activity, "not_public")

    # Defensive content size cap (settings-driven). Pydantic doesn't
    # enforce length on the str field by design — we want a clean
    # rejected reason instead of a noisy ValidationError surface.
    max_bytes = getattr(settings, "ACTIVITYPUB_INGEST_BODY_MAX_BYTES", 262_144)
    content_value = obj.get("content")
    if isinstance(content_value, str) and len(content_value.encode("utf-8")) > max_bytes:
        return _mark_rejected(federation_activity, "content_too_large")

    try:
        note = IngestedNote.model_validate(obj)
    except ValidationError as exc:
        # Compress the pydantic error structure into a single reason
        # string so the audit row stays scannable. The full validation
        # detail is on logger.warning for forensic recovery.
        logger.warning("ap.5e.schema_invalid errors=%s", exc.errors())
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = first.get("msg", "invalid")
        return _mark_rejected(federation_activity, f"schema:{loc}:{msg}")

    instance_host = _instance_host(activity)
    if not instance_host:
        return _mark_rejected(federation_activity, "missing_instance_host")

    canonical = canonicalize_link(note.url)
    if not canonical:
        return _mark_rejected(federation_activity, "canonical_link_empty")

    # Build a non-persisted candidate so find_duplicate has the same
    # shape it gets from JobPost.save / from_json. We do NOT save the
    # candidate before the dedup decision — saving early would create
    # the row even when we later decide to merge.
    title = note.name or (note.content.splitlines()[0] if note.content else "")
    title = title[:200] if title else None

    candidate = JobPost(
        title=title,
        description=note.content,
        link=note.url,
        location=None,
        source="activitypub",
        source_instance=instance_host,
        audience=[AS2_PUBLIC],
        complete=True,
        posted_date=note.published.date() if note.published else None,
        canonical_link=canonical,
    )

    existing = find_duplicate(candidate)
    if existing is not None:
        # Sticky-closed override — once a posting is closed locally
        # (either by the user or by the extractor's text-signal pass),
        # a remote refanout cannot reopen it. We REJECT rather than
        # silently dropping so the audit row carries the reason; the
        # remote actor will retry on its own schedule if it cares.
        if existing.posting_status == "closed":
            return _mark_rejected(federation_activity, "sticky_closed_local")

        _write_federated_merge_annotation(canonical, existing, activity)
        logger.info(
            "ap.5e.merged candidate_canonical=%s into local_jp=%s instance=%s",
            canonical, existing.pk, instance_host,
        )
        return IngestResult(outcome=OUTCOME_MERGED, job_post=existing)

    # Per-peer hourly quota — only charged on the CREATE branch. Merges
    # don't increment because a peer doing legitimate refanouts that
    # all land on existing local rows shouldn't lock themselves out of
    # creating new ones.
    if not _quota_check_and_increment(instance_host):
        return _mark_rejected(federation_activity, "instance_quota_exceeded")

    candidate.save()
    logger.info(
        "ap.5e.created jp=%s canonical=%s instance=%s activity_id=%s",
        candidate.pk, canonical, instance_host,
        activity.get("id") if isinstance(activity, dict) else None,
    )
    return IngestResult(outcome=OUTCOME_CREATED, job_post=candidate)


def replay_inbound_creates(limit: int = 100) -> dict:
    """Re-process previously-logged inbound Create activities.

    Operator tool: walks ``FederationActivity`` rows with
    ``direction=inbound, activity_type=Create`` whose
    ``delivery_status`` is still ``accepted`` (i.e. logged-by-5c, not
    yet ingested-by-5e) and runs ``ingest_create_note`` against each.
    Useful after toggling ``ACTIVITYPUB_INGEST_ENABLED`` from False to
    True, or for backfill after deploying 5e against a system that ran
    5c log-only for a while.

    Returns a tally dict: ``{"created": n, "merged": n, "rejected": n,
    "skipped": n, "error": n}``. ``error`` counts rows whose JSON body
    failed to re-parse — should be zero in practice.
    """
    qs = FederationActivity.objects.filter(
        direction="inbound",
        activity_type="Create",
        delivery_status="accepted",
    ).order_by("created_at")[:limit]
    tally = {
        OUTCOME_CREATED: 0,
        OUTCOME_MERGED: 0,
        OUTCOME_REJECTED: 0,
        OUTCOME_SKIPPED: 0,
        "error": 0,
    }
    for row in qs:
        try:
            activity = json.loads(row.body)
        except (ValueError, TypeError):
            tally["error"] += 1
            continue
        result = ingest_create_note(activity, federation_activity=row)
        tally[result.outcome] = tally.get(result.outcome, 0) + 1
    return tally
