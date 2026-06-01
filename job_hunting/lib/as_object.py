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

from datetime import datetime, time
from typing import Any

from django.conf import settings


AS2_CONTEXT = "https://www.w3.org/ns/activitystreams"

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
