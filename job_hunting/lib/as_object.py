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

import html
import re
import uuid
from collections import namedtuple
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


# ---------------------------------------------------------------------------
# BACK-96/97 — Note content builder (lean default + rich personalized).
#
# Replaces the old ``_render_note_content`` that echoed raw
# ``JobPost.description`` as ``<p>{description}</p>`` — the "actively
# hiring" defect. The body is now a line-composer with two formats:
#
#   LEAN (the AP default): 🟢 {title} — {company} / 📍 {location|Remote}
#     / 💰 {comp} / {hook}(real description only) / external link / hashtags
#   RICH (opt-in @dough show-off): LEAN + a {verdict} · {score} · applied line
#
# Gating + url precedence live with the activity builders below. Every
# segment is null-safe (drops cleanly, never renders "None"); the whole
# verdict line drops when verdict + score + applied are all absent. We
# NEVER emit AS2 ``summary`` — Mastodon reads it as a Content Warning and
# collapses the post.

# Per-user annotations the RICH format renders. Resolved from Score /
# JobApplication / JobApplicationStatus — NOT JobPost.top_score /
# _active_application_status (the two masking traps, see BACK-96).
PersonalAnnotations = namedtuple(
    "PersonalAnnotations", ["verdict", "reason_code", "score", "applied"]
)
_EMPTY_ANNOTATIONS = PersonalAnnotations(
    verdict=None, reason_code=None, score=None, applied=False
)

# Score bucket thresholds (locked with Doug): >=80 strong, >=60 good,
# else weak. Raw number always shown in parens: ``Strong match (87)``.
_SCORE_STRONG = 80
_SCORE_GOOD = 60

# Rendered-content budget. Mastodon counts a URL as a flat 23 chars
# regardless of its real length; we shrink the hook first to stay under.
_HARD_BUDGET = 500
_HOOK_MAX = 140
_HOOK_MIN = 20

# Seniority / filler words dropped when deriving a role hashtag so
# "Senior Platform Engineer" → #platformengineer (deterministic).
_HASHTAG_STOPWORDS = frozenset(
    {
        "senior", "junior", "sr", "jr", "staff", "lead", "principal",
        "the", "a", "an", "of", "and", "for", "to", "i", "ii", "iii", "iv",
    }
)

_TAG_RE = re.compile(r"<[^>]+>")


def resolve_personal_annotations_batch(job_posts, user_id):
    """Resolve ``PersonalAnnotations`` for ``user_id`` across ``job_posts``.

    Returns ``{job_post_pk: PersonalAnnotations}`` covering EVERY input pk
    (empty annotations when the user has no records) in a bounded, fixed
    number of queries regardless of page size — the Task D N+1 guard. The
    per-post path (:func:`_resolve_personal_annotations`) delegates here.

    Data sources + traps (BACK-96):
    - verdict = the LATEST ``Vetted Good`` / ``Vetted Bad``
      ``JobApplicationStatus`` on the user's application for the post —
      NOT the active-status rollup (``Applied`` masks the verdict).
    - score = the user's best ``Score`` for the post — NOT
      ``JobPost.top_score`` (request-scoped, no request here).
    - applied = the user has a ``JobApplication`` with
      ``applied_at`` set (a bare triage-created row is NOT "applied").
    """
    post_ids = [jp.pk for jp in job_posts]
    result = {pid: _EMPTY_ANNOTATIONS for pid in post_ids}
    if not post_ids or not user_id:
        return result

    from job_hunting.models import (
        JobApplication,
        JobApplicationStatus,
        Score,
    )

    verdict_map: dict = {}
    reason_map: dict = {}
    status_rows = (
        JobApplicationStatus.objects.filter(
            application__job_post_id__in=post_ids,
            application__user_id=user_id,
            status__status__in=("Vetted Good", "Vetted Bad"),
        )
        .select_related("status", "application")
        .order_by("application__job_post_id", "-logged_at", "-created_at")
    )
    for jas in status_rows:
        pid = jas.application.job_post_id
        if pid in verdict_map:
            continue
        verdict_map[pid] = jas.status.status if jas.status_id else None
        reason_map[pid] = jas.reason_code

    score_map: dict = {}
    score_rows = (
        Score.objects.filter(
            job_post_id__in=post_ids, user_id=user_id, score__isnull=False
        )
        .order_by("job_post_id", "-score")
        .values_list("job_post_id", "score")
    )
    for pid, score in score_rows:
        if pid not in score_map:
            score_map[pid] = score

    applied_ids = set(
        JobApplication.objects.filter(
            job_post_id__in=post_ids,
            user_id=user_id,
            applied_at__isnull=False,
        ).values_list("job_post_id", flat=True)
    )

    for pid in post_ids:
        result[pid] = PersonalAnnotations(
            verdict=verdict_map.get(pid),
            reason_code=reason_map.get(pid),
            score=score_map.get(pid),
            applied=pid in applied_ids,
        )
    return result


def _resolve_personal_annotations(job_post, user_id) -> PersonalAnnotations:
    """Single-post ``PersonalAnnotations`` resolver (delegates to the batch)."""
    return resolve_personal_annotations_batch([job_post], user_id).get(
        job_post.pk, _EMPTY_ANNOTATIONS
    )


def user_opted_into_rich(user_id) -> bool:
    """True when the user opted into the rich federated-Note format (Task B)."""
    if not user_id:
        return False
    from job_hunting.models import Profile

    prof = (
        Profile.objects.filter(user_id=user_id)
        .only("federate_rich", "user_id")
        .first()
    )
    return bool(prof and prof.federate_rich)


def _actor_rich_capable(actor) -> bool:
    """Actor-level half of the rich gate: a Person actor whose owning user
    opted into rich. Company / Organization actors are NEVER rich (no
    owner — would leak one user's private score onto a company page)."""
    if actor is None:
        return False
    from job_hunting.models.actor import ACTOR_TYPE_PERSON

    if getattr(actor, "type", None) != ACTOR_TYPE_PERSON:
        return False
    user_id = getattr(actor, "user_id", None)
    if user_id is None:
        return False
    return user_opted_into_rich(user_id)


def _should_render_rich(job_post, actor) -> bool:
    """Full rich gate: a rich-capable Person actor that OWNS this post."""
    if not _actor_rich_capable(actor):
        return False
    return getattr(actor, "user_id", None) == job_post.created_by_id


def _clean_text(value) -> str | None:
    """Strip tags + collapse whitespace; None / empty → None (null-safe)."""
    if not value:
        return None
    text = _TAG_RE.sub(" ", str(value))
    text = " ".join(text.split())
    return text or None


def _header_line(job_post) -> str | None:
    """``🟢 {title} — {company}`` — null-safe; drops to title-only or
    company-only, or omits entirely when both are absent."""
    title = _clean_text(job_post.title)
    company = None
    if job_post.company_id:
        company = _clean_text(getattr(getattr(job_post, "company", None), "name", None))
    if title and company:
        return f"🟢 {title} — {company}"
    if title:
        return f"🟢 {title}"
    if company:
        return f"🟢 {company}"
    return None


def _location_line(job_post) -> str | None:
    """``📍 {location}``; falls back to ``Remote`` when the post is flagged
    remote but carries no location string. Drops when neither applies —
    we never invent a location, and never render ``📍 None``."""
    location = _clean_text(job_post.location)
    if location:
        return f"📍 {location}"
    if job_post.remote:
        return "📍 Remote"
    return None


def _format_money(value) -> str | None:
    """Format a salary figure as ``$150k`` / ``$80`` (null-safe)."""
    if value is None:
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount >= 1000:
        return f"${round(amount / 1000)}k"
    return f"${amount}"


def _comp_line(job_post) -> str | None:
    """``💰 {comp}`` from salary_min / salary_max; drops when neither set."""
    lo = _format_money(job_post.salary_min)
    hi = _format_money(job_post.salary_max)
    if lo and hi:
        comp = lo if lo == hi else f"{lo}–{hi}"
    elif lo:
        comp = f"{lo}+"
    elif hi:
        comp = f"up to {hi}"
    else:
        return None
    return f"💰 {comp}"


def _hook_source(job_post) -> str | None:
    """Real-description hook text, or None for a thin stub.

    ``complete is False`` is the canonical thin-stub signal (cc_auto email
    pipeline / ReviewCompleteness / manual "mark incomplete"). Dropping
    the hook for stubs is precisely what kills the original "This company
    is actively hiring, based in Seattle, WA." toot."""
    if not getattr(job_post, "complete", True):
        return None
    return _clean_text(job_post.description)


def _truncate_hook(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` visible chars, ellipsizing on a cut."""
    if len(text) <= limit:
        return text
    cut = max(0, limit - 1)
    return text[:cut].rstrip() + "…"


def _verdict_segment(annotations: PersonalAnnotations) -> str | None:
    """``✅ Vetted good`` / ``❌ Vetted bad (reason_code)`` — never the note."""
    if annotations.verdict == "Vetted Good":
        return "✅ Vetted good"
    if annotations.verdict == "Vetted Bad":
        if annotations.reason_code:
            return f"❌ Vetted bad ({annotations.reason_code})"
        return "❌ Vetted bad"
    return None


def _score_segment(score) -> str | None:
    """``Strong match (87)`` — bucket label via 80/60 + raw number."""
    if score is None:
        return None
    if score >= _SCORE_STRONG:
        label = "Strong match"
    elif score >= _SCORE_GOOD:
        label = "Good match"
    else:
        label = "Weak match"
    return f"{label} ({score})"


def _verdict_line(annotations: PersonalAnnotations) -> str | None:
    """``{verdict} · {score} · applied`` — each segment null-safe; the whole
    line drops when verdict + score + applied are all absent."""
    if annotations is None:
        return None
    segments = []
    verdict = _verdict_segment(annotations)
    if verdict:
        segments.append(verdict)
    score = _score_segment(annotations.score)
    if score:
        segments.append(score)
    if annotations.applied:
        segments.append("applied")
    if not segments:
        return None
    return " · ".join(segments)


def _build_hashtags(job_post) -> str | None:
    """Deterministic, null-safe hashtag line: ``#hiring`` + a remote tag +
    a role tag derived from the title (seniority words stripped)."""
    tags = ["#hiring"]
    location = _clean_text(job_post.location)
    if job_post.remote or (location and "remote" in location.lower()):
        tags.append("#remotejobs")
    title = _clean_text(job_post.title)
    if title:
        words = [
            re.sub(r"[^a-z0-9]", "", w.lower())
            for w in title.split()
        ]
        words = [
            w for w in words if w and w not in _HASHTAG_STOPWORDS
        ]
        role = "".join(words)[:30]
        if role:
            tags.append(f"#{role}")
    # Dedupe while preserving order (a title word could collide with a tag).
    seen = set()
    ordered = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return " ".join(ordered) if ordered else None


def _compose_note_content(
    job_post, *, rich: bool = False, annotations: PersonalAnnotations | None = None
) -> str | None:
    """Compose the AS2 ``content`` HTML for a JobPost Note (lean or rich).

    Lines are emitted in order — header / location / comp / hook /
    (rich) verdict / url / hashtags — each one null-safe and dropped when
    empty. The hook is sized last against the 500-char budget (URL counts
    23) so it shrinks before anything structural is lost."""
    header = _header_line(job_post)
    location = _location_line(job_post)
    comp = _comp_line(job_post)
    verdict = _verdict_line(annotations) if (rich and annotations) else None
    url = _resolve_human_url(job_post)
    hashtags = _build_hashtags(job_post)
    hook_source = _hook_source(job_post)

    # Structural (non-hook) lines, with their visible length. A URL line
    # is counted as a flat 23 per Mastodon's link accounting.
    structural: list[tuple[str, int]] = []
    for line in (header, location, comp):
        if line:
            structural.append((line, len(line)))
    hook_slot = len(structural)  # hook is inserted at this index
    tail: list[tuple[str, int]] = []
    if verdict:
        tail.append((verdict, len(verdict)))
    if url:
        tail.append((url, 23))
    if hashtags:
        tail.append((hashtags, len(hashtags)))

    hook_text = None
    if hook_source:
        base_visible = sum(v for _, v in structural) + sum(v for _, v in tail)
        n_lines = len(structural) + len(tail) + 1  # +1 for the hook line
        allowance = _HARD_BUDGET - base_visible - (n_lines - 1)
        hook_len = max(0, min(_HOOK_MAX, allowance))
        if hook_len >= _HOOK_MIN:
            hook_text = _truncate_hook(hook_source, hook_len)

    ordered = [line for line, _ in structural]
    if hook_text:
        ordered.insert(hook_slot, hook_text)
    ordered.extend(line for line, _ in tail)

    if not ordered:
        return None
    return "<p>" + "<br>".join(html.escape(line) for line in ordered) + "</p>"


def _resolve_human_url(job_post) -> str | None:
    """Human "view original" URL precedence (BACK-97): a RESOLVED
    ``apply_url`` → ``canonical_link`` → ``link``. The internal
    ``/job-posts/<pk>`` floor is intentionally dropped — JP pages aren't
    human-public; the AS2 object ``id`` keeps that URI for machines."""
    apply_url = job_post.apply_url
    if apply_url and job_post.apply_url_status == "resolved":
        return apply_url
    if job_post.canonical_link:
        return job_post.canonical_link
    if job_post.link:
        return job_post.link
    return None


def build_jobpost_note(
    job_post,
    actor_uri_str: str,
    *,
    rich: bool = False,
    annotations: PersonalAnnotations | None = None,
) -> dict:
    """The single Note-object builder Create / Update / standalone-fetch all
    route through (the BACK-96 chokepoint). Sets ``content`` from the
    lean/rich composer and ``url`` from the resolved-apply precedence;
    NEVER sets ``summary``. ``id`` stays the AS2 object URI."""
    note: dict[str, Any] = {
        "id": object_uri(job_post),
        "type": "Note",
        "attributedTo": actor_uri_str,
        "to": [AS2_PUBLIC],
    }
    published = _isoformat(job_post.created_at)
    if published:
        note["published"] = published
    content = _compose_note_content(job_post, rich=rich, annotations=annotations)
    if content:
        note["content"] = content
    url = _resolve_human_url(job_post)
    if url:
        note["url"] = url
    return note


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


def _resolve_rich_and_annotations(job_post, actor, rich, annotations):
    """Normalize the (rich, annotations) pair for a single-post build.

    ``rich=None`` → derive it from the actor gate. When rich and no
    annotations were supplied (the single-post Create/Update/standalone
    path), resolve them per-post; the batch outbox path passes them in
    pre-resolved so the per-Note builders never re-query (Task D)."""
    if rich is None:
        rich = _should_render_rich(job_post, actor)
    if rich and annotations is None:
        annotations = _resolve_personal_annotations(job_post, job_post.created_by_id)
    return rich, annotations


def build_create_activity_for_jobpost(
    job_post, actor, *, rich=None, annotations=None
) -> dict:
    """Build the AS2 ``Create(Note)`` activity envelope for ``job_post``.

    Reused by the Phase 5b outbox view and the Phase 5d dispatch worker
    so both surfaces emit byte-identical activities for the same JobPost.
    The Note body routes through :func:`build_jobpost_note` (the BACK-96
    content chokepoint): lean by default, rich (verdict/score/applied)
    when ``actor`` is the owning Person actor whose user opted in.

    ``rich`` / ``annotations`` may be supplied by a batch caller (the
    outbox, Task D) so per-page rendering stays O(1) in query count;
    otherwise they're resolved per-post here.

    ``actor`` is the local ``Actor`` row (Phase 5a model); the activity
    is attributed to ``actor.uri``. ``to`` is the AS2 Public collection,
    ``cc`` points at the actor's followers collection.
    """
    origin = _instance_origin()
    actor_uri_str = f"{origin}/actors/{actor.preferred_username}"
    published = _isoformat(job_post.created_at)

    rich, annotations = _resolve_rich_and_annotations(
        job_post, actor, rich, annotations
    )
    note = build_jobpost_note(
        job_post, actor_uri_str, rich=rich, annotations=annotations
    )

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


def _note_for_jobpost(
    job_post, actor_uri_str: str, *, rich: bool = False, annotations=None
) -> dict:
    """Return the Note object for a JobPost — the shape both 5b Create and
    5d Update wrap. Thin alias over :func:`build_jobpost_note` so the
    lean/rich content + url logic has exactly one home (BACK-96)."""
    return build_jobpost_note(
        job_post, actor_uri_str, rich=rich, annotations=annotations
    )


def build_note_object_for_jobpost(job_post, actor=None, *, annotations=None) -> dict:
    """Standalone, dereferenceable AS2 Note for a JobPost object id.

    Served at the public ``/job-posts/<pk>`` object URI so a federation
    peer that dereferences an outbox / delivered ``Create.object.id``
    gets the Note itself, not the SPA HTML (the BACK-93 defect). The
    shape mirrors the note embedded in
    :func:`build_create_activity_for_jobpost` — same ``id``,
    ``attributedTo`` (the owning actor's ``/actors/<preferred_username>``
    URI), ``to`` / ``content`` / ``url`` — PLUS the top-level
    ``@context`` a standalone fetched document requires. The embedded
    note inherits ``@context`` from its Create envelope; a bare fetch
    does not, so we add it here.

    ``actor`` is the owning ``Actor`` row when known, so ``attributedTo``
    is byte-identical to what the outbox advertised. When None (no Actor
    row materialized yet) it falls back to the author's username — the
    same handle :func:`actor_uri` mints elsewhere — so the document is
    still self-consistent.
    """
    if actor is not None:
        actor_uri_str = _local_actor_uri(actor)
        rich = _should_render_rich(job_post, actor)
    else:
        # No Actor row to authenticate ownership against → lean only. We
        # can't confirm a rich-opted-in Person owner, and must not leak
        # private vetting on a bare anonymous-attributed fetch.
        author = getattr(job_post, "created_by", None)
        handle = getattr(author, "username", None) or "anonymous"
        actor_uri_str = actor_uri(handle, job_post.source_instance)
        rich = False
    rich, annotations = _resolve_rich_and_annotations(
        job_post, actor, rich, annotations
    )
    note = _note_for_jobpost(
        job_post, actor_uri_str, rich=rich, annotations=annotations
    )
    return {"@context": AS2_CONTEXT, **note}


def build_update_activity_for_jobpost(
    job_post, actor, *, edit_marker=None, rich=None, annotations=None
) -> dict:
    """Build the AS2 ``Update(Note)`` activity envelope for ``job_post``.

    Phase 5d worker entry point. Mirrors ``build_create_activity_for_jobpost``
    but wraps the same Note in an ``Update`` activity and derives a
    per-edit activity id so subsequent edits don't collide with one
    another (Create's id deliberately doesn't change across edits — it
    pins to the JobPost identity, not the revision). Carries the same
    lean/rich body as the Create (BACK-96) so a vet/score/apply Update
    is what actually surfaces the rich data on the fediverse (BACK-99).

    ``edit_marker`` is the discriminator folded into the activity id.
    Caller passes JobPost.updated_at when available; falls back to a
    fresh ISO-now string so the id is still distinct on systems whose
    JobPost row predates the (not-yet-added) updated_at column.
    """
    origin = _instance_origin()
    actor_uri_str = _local_actor_uri(actor)
    rich, annotations = _resolve_rich_and_annotations(
        job_post, actor, rich, annotations
    )
    note = _note_for_jobpost(
        job_post, actor_uri_str, rich=rich, annotations=annotations
    )

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
