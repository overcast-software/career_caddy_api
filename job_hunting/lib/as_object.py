"""ActivityStreams 2.0 JSON-LD adapter for JobPost.

Phase 4 of the ActivityPub-prep plan. The adapter returns a JSON-LD
document that downstream ActivityPub consumers can ingest — Mastodon /
Lemmy / a future Career Caddy federation peer. No dispatch, no Outbox,
no HTTP signatures: this is round-trip serialization only, so the data
shape can be validated against external AS2 implementations before we
commit to the full federation protocol.

The shape mirrors a Note (AS2's most-compatible content type) with
ActivityPub additions for ``attributedTo`` and ``audience``. A custom
``careercaddy:`` prefix carries job-board-specific fields (apply URL,
location, source) that don't map onto AS2 directly.

The adapter is *latent*: it has no UI, no link from elsewhere in the
API, and no caller in core. Phase 5 federation work will activate it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from typing import Any

from django.conf import settings


AS2_CONTEXT = "https://www.w3.org/ns/activitystreams"

# AS2 magic URI for the public-collection — anything addressed `to`
# (or `cc`'d) this URI is fully public, federable, and crawlable.
AS2_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"

# Stable UUID5 namespace for the Phase 5b ``Create`` activity IDs the
# outbox emits. Phase 5b deliberately does NOT persist Activity rows —
# the dispatcher in Phase 5d will. Until then we derive activity URIs
# from ``uuid5(NS, "create:<jobpost.id>")`` so the same JobPost yields
# the same activity URI across requests (idempotency for any peer that
# caches by ``id``) without needing a DB column.
ACTIVITYPUB_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NS_URL

# Custom vocabulary terms for job-board-specific fields that AS2
# doesn't model. Namespaced under a careercaddy: prefix so federation
# peers can ignore them without breaking the AS2 envelope.
CAREER_CADDY_NS = "https://careercaddy.online/ns#"


def _instance_origin(instance: str | None = None) -> str:
    """Return the origin (``scheme://host[:port]``) for URI construction.

    Single point of truth for the host portion of every URI the adapter
    emits — actor IDs, object IDs, and any inline URIs all share this
    origin so federation peers see a consistent identity.

    Resolution order:
    1. Explicit ``instance`` arg (for federated rows whose
       ``source_instance`` differs from this server) — always
       https-prefixed; remote peers are expected to terminate TLS.
    2. ``settings.INSTANCE_ORIGIN`` — full origin, scheme included.
       Phase 5a addition; lets local-dev / Mastodon harness drive
       http://localhost:8000 or http://api:8000 without lying about
       scheme in the emitted URIs.
    3. ``https://{settings.CAREER_CADDY_INSTANCE}`` — Phase 4 fallback,
       preserved so the existing as_object tests + any deploy that
       hasn't set INSTANCE_ORIGIN yet keep working.
    """
    local_host = settings.CAREER_CADDY_INSTANCE
    if instance and instance != local_host:
        # Foreign row — preserve origin URI from its source instance.
        return f"https://{instance}"
    origin = getattr(settings, "INSTANCE_ORIGIN", None)
    if origin:
        return origin.rstrip("/")
    return f"https://{local_host}"


def actor_uri(username: str, instance: str | None = None) -> str:
    """Return the AS2 Actor URI for ``username`` on ``instance``.

    Stub: the actor object itself doesn't exist yet (Phase 5). The URI
    shape is fixed now so the ``attributedTo`` field can point at a
    stable identity. Email-style actor IDs (``acct:user@instance``) are
    rejected by some ActivityPub clients; an https URI is the safer
    choice for forward-compat.
    """
    return f"{_instance_origin(instance)}/actors/{username}"


def object_uri(job_post) -> str:
    """Return the canonical AS2 ``id`` URI for a JobPost.

    Always rooted on ``job_post.source_instance``, NOT the current
    instance — so a federated JobPost retains its origin URI when
    relayed through us. For local rows the two are identical by
    construction.
    """
    return f"{_instance_origin(job_post.source_instance)}/job-posts/{job_post.pk}"


def _isoformat(value: Any) -> str | None:
    """Coerce a date / datetime to an ISO8601 string. None passes through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # Assume date — promote to midnight UTC so the resulting string
    # round-trips through every AS2 implementation we've spot-checked.
    return datetime.combine(value, time.min).isoformat()


def job_post_as_object(job_post) -> dict:
    """Build the AS2 Note object for ``job_post``.

    Defensive about missing data: a JobPost with no description / no
    title / no posted_date still produces a valid AS2 document with
    those keys omitted, because external clients reject documents that
    carry null where strings are expected.
    """
    author = getattr(job_post, "created_by", None)
    author_handle = (
        getattr(author, "username", None) or "anonymous"
    )

    out: dict[str, Any] = {
        "@context": [
            AS2_CONTEXT,
            {"careercaddy": CAREER_CADDY_NS},
        ],
        "type": "Note",
        "id": object_uri(job_post),
        "attributedTo": actor_uri(author_handle, job_post.source_instance),
    }

    # Audience → to / cc split. AS2's `to` is for primary recipients
    # (the public collection lives here for public posts); `audience`
    # mirrors the raw list so downstream consumers can do their own
    # routing. Empty audience = private to the actor; emit neither
    # `to` nor `cc` so the post is delivered nowhere by default.
    audience = job_post.audience if isinstance(job_post.audience, list) else []
    if audience:
        out["to"] = list(audience)
        out["audience"] = list(audience)

    if job_post.title:
        out["name"] = job_post.title
    if job_post.description:
        # AS2 `content` is HTML; the description field is rendered as
        # plain text in core UI but stored as raw HTML/markdown mix.
        # External AS2 clients will sanitize on display — emitting as-is
        # is the convention.
        out["content"] = job_post.description

    published = _isoformat(job_post.posted_date) or _isoformat(job_post.created_at)
    if published:
        out["published"] = published

    if job_post.link:
        out["url"] = job_post.link

    location = job_post.location
    if location:
        # AS2 has a structured `location` object; the typed-Place form
        # is heavier than what core has captured (we only know the
        # string). Emit the Place with a `name` so federation peers
        # render something readable.
        out["location"] = {"type": "Place", "name": location}

    # Career-Caddy-namespaced fields that don't map to AS2 vocab.
    # Wrapped under a single key so downstream readers can branch
    # on prefix presence rather than detecting each field.
    cc_extension: dict[str, Any] = {
        "source": job_post.source,
        "sourceInstance": job_post.source_instance,
    }
    if job_post.apply_url:
        cc_extension["applyUrl"] = job_post.apply_url
    if job_post.canonical_link:
        cc_extension["canonicalLink"] = job_post.canonical_link
    if job_post.company_id and getattr(job_post.company, "name", None):
        cc_extension["company"] = job_post.company.name
    if job_post.posting_status:
        cc_extension["postingStatus"] = job_post.posting_status

    out["careercaddy:extension"] = cc_extension

    return out


def _create_activity_uuid(job_post) -> str:
    """Deterministic UUID5 for the ``Create`` activity wrapping ``job_post``.

    Phase 5b ships read-only outbox rendering; we don't persist activity
    rows yet (5d's territory). Deriving the activity UUID from the
    JobPost id means two consecutive ``GET /outbox?page=N`` requests
    return identical ``id`` URIs for the same item — which any peer
    that caches activities by id will rely on.
    """
    return str(uuid.uuid5(ACTIVITYPUB_NAMESPACE, f"create:{job_post.pk}"))


def _render_note_content(job_post) -> str | None:
    """Return the HTML body for the wrapped Note, or None if empty.

    AS2's ``content`` is HTML — peers (Mastodon especially) sanitise on
    display, so we emit the JobPost description as-is when it looks
    like HTML and wrap plain text in a single ``<p>``. The detection
    is shape-only: if the field contains an angle bracket, trust it as
    pre-rendered markup. Otherwise paragraph-wrap. The slim-payload
    guard (per the Phase 5b plan): no per-row model lookups — only
    fields already on the JobPost instance.
    """
    description = job_post.description
    if not description:
        return None
    if "<" in description and ">" in description:
        return description
    return f"<p>{description}</p>"


def _activity_uuid(kind: str, discriminator: str) -> str:
    """Deterministic UUID5 for the activity envelope of ``kind`` + ``discriminator``.

    Phase 5d additions parallel the 5b ``_create_activity_uuid`` helper.
    The discriminator is what makes two activities of the same kind +
    JobPost distinct — for Create that's the JobPost id alone (one
    Create per JobPost), but Update can fire many times across a row's
    lifetime so it folds in a per-edit timestamp.
    """
    return str(uuid.uuid5(ACTIVITYPUB_NAMESPACE, f"{kind}:{discriminator}"))


def _local_actor_uri(actor) -> str:
    """Local helper — actor URI shape mirrors signing module's `_local_actor_uri`."""
    return f"{_instance_origin()}/actors/{actor.preferred_username}"


def build_create_activity_for_jobpost(job_post, actor) -> dict:
    """Build the AS2 ``Create(Note)`` activity envelope for ``job_post``.

    Reused by the Phase 5b outbox view and the Phase 5d dispatch worker
    (when it lands) so both surfaces emit byte-identical activities for
    the same JobPost — which matters once peers start signing the
    fetched objects and we have to round-trip through deduplication on
    redelivery.

    ``actor`` is the local ``Actor`` row (Phase 5a model); the activity
    is attributed to ``actor.uri`` (mirrors the outbox advertising URL).
    Per the Phase 5b plan: ``to`` is the AS2 Public collection, ``cc``
    points at the actor's followers collection so future follower
    fan-out (5d) can address them implicitly.
    """
    origin = _instance_origin()
    actor_uri_str = f"{origin}/actors/{actor.preferred_username}"
    published = _isoformat(job_post.created_at)

    note: dict[str, Any] = {
        "id": object_uri(job_post),
        "type": "Note",
        "attributedTo": actor_uri_str,
        "to": [AS2_PUBLIC],
    }
    if published:
        note["published"] = published

    content = _render_note_content(job_post)
    if content:
        note["content"] = content

    # Prefer the post's canonical (deduped) link as the human-resolvable
    # URL — peer UIs surface this as the "view original" link. Fall back
    # to the raw link when canonicalisation hasn't run yet.
    url = job_post.canonical_link or job_post.link
    if url:
        note["url"] = url

    activity: dict[str, Any] = {
        "@context": AS2_CONTEXT,
        "id": f"{origin}/activities/{_create_activity_uuid(job_post)}",
        "type": "Create",
        "actor": actor_uri_str,
        "to": [AS2_PUBLIC],
        "cc": [f"{actor_uri_str}/followers"],
        "object": note,
    }
    if published:
        activity["published"] = published

    return activity


def _note_for_jobpost(job_post, actor_uri_str: str) -> dict:
    """Return the Note object for a JobPost — the shape both 5b Create
    and 5d Update wrap.

    Kept tiny and pure (no env / settings reads) so the wrappers around
    it can drift independently (e.g. Update may want different ``cc``
    targeting than Create). Doesn't unify the Phase 4 ``job_post_as_object``
    helper — that one carries the ``careercaddy:`` extension namespace
    and renders for the latent /as-object/ adapter, not for activity
    envelopes that hit federation peers.
    """
    note: dict[str, Any] = {
        "id": object_uri(job_post),
        "type": "Note",
        "attributedTo": actor_uri_str,
        "to": [AS2_PUBLIC],
    }
    published = _isoformat(job_post.created_at)
    if published:
        note["published"] = published
    content = _render_note_content(job_post)
    if content:
        note["content"] = content
    url = job_post.canonical_link or job_post.link
    if url:
        note["url"] = url
    return note


def build_update_activity_for_jobpost(job_post, actor, *, edit_marker=None) -> dict:
    """Build the AS2 ``Update(Note)`` activity envelope for ``job_post``.

    Phase 5d worker entry point. Mirrors ``build_create_activity_for_jobpost``
    but wraps the same Note in an ``Update`` activity and derives a
    per-edit activity id so subsequent edits don't collide with one
    another (Create's id deliberately doesn't change across edits — it
    pins to the JobPost identity, not the revision).

    ``edit_marker`` is the discriminator folded into the activity id.
    Caller passes JobPost.updated_at when available; falls back to a
    fresh ISO-now string so the id is still distinct on systems whose
    JobPost row predates the (not-yet-added) updated_at column.
    """
    origin = _instance_origin()
    actor_uri_str = _local_actor_uri(actor)
    note = _note_for_jobpost(job_post, actor_uri_str)

    if edit_marker is None:
        edit_marker = datetime.now(tz=timezone.utc).isoformat()
    elif isinstance(edit_marker, datetime):
        edit_marker = edit_marker.isoformat()
    else:
        edit_marker = str(edit_marker)
    discriminator = f"{job_post.pk}:{edit_marker}"

    activity: dict[str, Any] = {
        "@context": AS2_CONTEXT,
        "id": f"{origin}/activities/{_activity_uuid('update', discriminator)}",
        "type": "Update",
        "actor": actor_uri_str,
        "to": [AS2_PUBLIC],
        "cc": [f"{actor_uri_str}/followers"],
        "object": note,
    }
    published = _isoformat(job_post.created_at)
    if published:
        activity["published"] = published
    return activity


def build_delete_activity_for_jobpost(job_post, actor) -> dict:
    """Build the AS2 ``Delete(Tombstone)`` activity envelope for ``job_post``.

    Phase 5d worker entry point. The ``object`` is a Tombstone wrapper
    rather than a bare URI — Mastodon and Pleroma both prefer Tombstone
    so they can record the formerType for future moderation surfaces.
    Activity id is deterministic in JobPost id alone: deletes are
    idempotent on the receiving end, so a re-attempted Delete should
    carry the same id and dedupe at the peer.
    """
    origin = _instance_origin()
    actor_uri_str = _local_actor_uri(actor)
    object_id = object_uri(job_post)

    tombstone: dict[str, Any] = {
        "id": object_id,
        "type": "Tombstone",
        "formerType": "Note",
    }

    activity: dict[str, Any] = {
        "@context": AS2_CONTEXT,
        "id": f"{origin}/activities/{_activity_uuid('delete', str(job_post.pk))}",
        "type": "Delete",
        "actor": actor_uri_str,
        "to": [AS2_PUBLIC],
        "cc": [f"{actor_uri_str}/followers"],
        "object": tombstone,
    }
    return activity
