"""ActivityPub Phase 5e + 6b — federated JobPost ingestion + dedup.

Inbox-side counterpart to ``lib/federation_dispatch`` (5d): inbound
``Create(Note)`` activities verified by the 5c inbox handler arrive
here, get defensively parsed against ``IngestedNote``, walk the dedup
decision tree, and either:

  * CREATED — new JobPost row inserted with ``source="federation"``
    and ``source_instance`` set to the remote peer's host. Phase 6b
    standardised the source label (was ``"activitypub"`` under 5e —
    the 0107 data migration rewrote existing rows).
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
    Company,
    DuplicateAnnotation,
    FederationActivity,
    JobPost,
    JobPostDiscovery,
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


# ---------------------------------------------------------------------------
# Phase 6b — careercaddy:extension AS2 coercion + content sanitization.
#
# The AS2 vocab carries our domain-specific fields under the
# ``careercaddy:extension`` namespace (mirrored to the outbound shape
# in ``lib/as_object``). On ingest we pull them off, sanitize through
# defensive helpers, and merge into the candidate JobPost.
#
# Sanitization shape: bleach isn't installed in api/, so the HTML strip
# goes through BeautifulSoup (already a dependency for the scraper). We
# extract text content only — peers can't smuggle <script> or onclick
# handlers since we never render their HTML back. ``html.escape`` would
# leave the literal markup readable to the user, which we don't want.

_EXTENSION_KEY = "careercaddy:extension"
# Cap on the title field. Mirrors the model's ``title = CharField(max_length=255)``
# bound but with a slightly smaller window so over-long titles get clipped
# cleanly during sanitization rather than blowing up the DB constraint.
_TITLE_MAX_CHARS = 255
# Cap on the description payload. 50 KB matches the Phase 6b plan's
# explicit ceiling; the activity-level content-too-large gate
# (``ACTIVITYPUB_INGEST_BODY_MAX_BYTES``, default 256 KB) is the wider
# envelope check, this is the field-specific clip.
_DESCRIPTION_MAX_BYTES = 50 * 1024


def _strip_html(value: str | None) -> str | None:
    """Return ``value`` with all HTML tags stripped, scripts removed.

    Peer instances commonly send ``<p>...</p>`` markup in ``object.content``
    (Mastodon's default). We don't render that HTML — JobPost
    descriptions surface as plain text in the frontend's detail view
    today. Stripping tags up-front prevents any future renderer change
    from accidentally exposing the smuggled markup.

    Uses BeautifulSoup's ``get_text`` to drop tags + script/style
    contents in a single pass. Falls back to the input unchanged on
    parser exceptions — better to keep the noisy original than to
    discard the content on a parser quirk.
    """
    if not value:
        return value
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(value, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception:  # pragma: no cover - defensive
        logger.exception("ap.6b.strip_html_failed")
        return value


def _clip_title(value: str | None) -> str | None:
    """Trim title to ``_TITLE_MAX_CHARS``, return None for empty input."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:_TITLE_MAX_CHARS]


def _clip_description(value: str | None) -> str | None:
    """Trim description to ``_DESCRIPTION_MAX_BYTES`` byte-budget.

    Byte-based clip (not char-based) because the 50 KB ceiling is about
    storage + network cost, both of which key on bytes. UTF-8 truncation
    backs up to the previous codepoint boundary so we don't slice a
    multi-byte character in half.
    """
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= _DESCRIPTION_MAX_BYTES:
        return value
    trimmed = encoded[:_DESCRIPTION_MAX_BYTES]
    # Walk back to the last full UTF-8 codepoint. The high-bit pattern
    # 10xxxxxx signals a continuation byte; trim until the trailing byte
    # is a single-byte (0xxxxxxx) or leading-byte (11xxxxxx) so the
    # decode doesn't blow up.
    while trimmed and (trimmed[-1] & 0xC0) == 0x80:
        trimmed = trimmed[:-1]
    return trimmed.decode("utf-8", errors="ignore")


def _extension_payload(obj: dict) -> dict:
    """Return the ``careercaddy:extension`` payload from an AS2 object.

    Defensive against missing / malformed extension blocks. Returns an
    empty dict so callers can ``.get("apply_url")`` etc. without a
    None-check at every site.
    """
    if not isinstance(obj, dict):
        return {}
    payload = obj.get(_EXTENSION_KEY)
    if not isinstance(payload, dict):
        return {}
    return payload


def _resolve_company(extension: dict) -> Company | None:
    """Resolve or create a Company from the ``careercaddy:extension`` block.

    Two-stage lookup:

    1. ``name_slug`` match (the Phase A alias key — ``slug(strip_corp_suffix(name))``).
       If a local Company already has the slug, return it. This is the
       common case for federated re-broadcasts of well-known employers.
    2. No match → create a fresh Company with ``source="federation"``
       and ``federation_enabled=False`` (Q2 in the Phase 6 plan: opt-in
       per Company, so a federation-minted row doesn't auto-publish
       back out without staff review).

    Returns None when the extension block carries no usable company
    name — the JobPost row lands without a Company FK, which is the
    same shape it gets from a thin-stub email-forward ingest.
    """
    name = extension.get("company") if isinstance(extension, dict) else None
    if isinstance(name, dict):
        # Some peers carry the company as a nested object {"name": ...}
        # rather than a bare string. Be forgiving.
        name = name.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None

    # Path 1 — exact-by-slug. Reuses the dedupe key Phase A landed.
    try:
        from job_hunting.lib.slug import slug, strip_corp_suffix
    except Exception:  # pragma: no cover - defensive
        return None
    candidate_slug = slug(strip_corp_suffix(name))
    if candidate_slug:
        existing = (
            Company.objects.filter(name_slug=candidate_slug)
            .order_by("id")
            .first()
        )
        if existing is not None:
            # If this row is an alias, prefer its canonical so the JP
            # attaches to the true root — mirrors how Phase A
            # ``find_by_alias`` callers handle the chain.
            if existing.canonical_id:
                return existing.canonical
            return existing

    # Path 2 — mint a fresh row. ``name`` carries an alphabet uniqueness
    # constraint at the DB level; if a row with this exact name already
    # exists we re-fetch instead of crashing on the integrity error.
    try:
        return Company.objects.create(
            name=name,
            display_name=name,
            source=Company.SOURCE_FEDERATION,
            name_slug=candidate_slug or None,
            federation_enabled=False,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("ap.6b.company_create_failed name=%s", name)
        return Company.objects.filter(name=name).first()


def _create_discoveries_for_company_actor(
    job_post: JobPost,
    attributed_to: str | None,
) -> int:
    """Create a JobPostDiscovery row for each follower of the targeted
    Company actor. Returns the number of rows created.

    The targeted Company actor URI shape is ``{origin}/companies/<slug>``
    (Phase 6a). When the inbound Note's ``attributedTo`` matches one,
    we resolve the local Company by slug and look up active
    ``FederationFollower`` rows keyed off that ``company_id`` with a
    ``local_user`` also set — the Phase 6b "local user subscribing to
    a Company actor" shape. Discoveries need a local User to attribute
    against; remote-only follower rows (``local_user=NULL``,
    ``company=set``) carry no local owner so they don't materialize a
    discovery here. Once 6d (employer self-claim) lands, the
    Company → claiming-user link can be backfilled into existing
    rows and the discoveries will flow.

    Idempotent — uses ``get_or_create`` with the unique constraint on
    (job_post, user).
    """
    if not attributed_to or not isinstance(attributed_to, str):
        return 0
    # Only auto-discover for locally-attributed Company actors. A remote
    # Company actor's discoveries are the peer instance's concern, not
    # ours — we only have a follower list for our own actors.
    from django.conf import settings as _settings
    origin = getattr(_settings, "INSTANCE_ORIGIN", "").rstrip("/")
    if not origin or not attributed_to.startswith(f"{origin}/companies/"):
        return 0
    slug_value = attributed_to[len(f"{origin}/companies/"):].rstrip("/")
    if not slug_value:
        return 0
    company = Company.objects.filter(slug=slug_value).first()
    if company is None:
        return 0

    from job_hunting.models import FederationFollower as _FF

    # Phase 6b — active followers keyed off the Company FK. Remote
    # followers carry no local_user (they're remote accounts); the
    # discovery write only fires for the legacy shape where a local
    # user followed the Company actor (a path that doesn't exist in
    # production yet but is wired here so 6d can land without
    # re-touching this code).
    follower_user_ids = (
        _FF.objects.filter(
            company_id=company.id,
            unfollowed_at__isnull=True,
            local_user_id__isnull=False,
        )
        .values_list("local_user_id", flat=True)
        .distinct()
    )

    created = 0
    for user_id in follower_user_ids:
        if user_id is None:
            continue
        _, was_new = JobPostDiscovery.objects.get_or_create(
            job_post=job_post,
            user_id=user_id,
            defaults={"source": "federation"},
        )
        if was_new:
            created += 1
    return created


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

    # Phase 6b — reply Notes are out of scope for 6b ingest. ``inReplyTo``
    # signals a comment-style activity that Phase 7c will plumb through
    # a JobPostComment ingest path. Drop here as SKIPPED so the audit
    # row stays accepted and the activity isn't mistaken for a JP
    # candidate.
    if obj.get("inReplyTo"):
        return IngestResult(
            outcome=OUTCOME_SKIPPED, reason="in_reply_to_not_yet_supported"
        )

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

    # Phase 6b — coerce ``careercaddy:extension`` fields off the AS2
    # object. ``canonical_link`` from the extension wins over the bare
    # ``note.url`` canonicalization when both are present: the peer is
    # asserting a host-specific canonical form (post-rewrite, post-
    # tracking-strip) that's strictly more precise than what we'd
    # rebuild from the URL alone.
    extension = _extension_payload(obj)
    ext_canonical = extension.get("canonical_link") if isinstance(extension, dict) else None
    canonical = (
        canonicalize_link(ext_canonical) if isinstance(ext_canonical, str) and ext_canonical
        else canonicalize_link(note.url)
    )
    if not canonical:
        return _mark_rejected(federation_activity, "canonical_link_empty")

    # Sanitize + cap the human-visible fields before persistence so a
    # peer can't smuggle HTML / oversized payloads past the field-level
    # contract. The body-level cap upstream is the wider envelope
    # check.
    raw_title = note.name or (
        (note.content or "").splitlines()[0] if note.content else ""
    )
    title = _clip_title(_strip_html(raw_title))
    description = _clip_description(_strip_html(note.content))

    ext_apply_url = extension.get("apply_url") if isinstance(extension, dict) else None
    if not isinstance(ext_apply_url, str) or not ext_apply_url.strip():
        ext_apply_url = None
    ext_posting_status = (
        extension.get("posting_status") if isinstance(extension, dict) else None
    )
    if ext_posting_status not in {"open", "closed"}:
        ext_posting_status = None

    # Company resolution: lookup by name_slug → existing row; else mint
    # a fresh row with source="federation". None when the extension
    # carries no usable company name; matches the thin-stub shape.
    company = _resolve_company(extension)

    candidate = JobPost(
        title=title,
        description=description,
        link=note.url,
        location=None,
        source="federation",
        source_instance=instance_host,
        audience=[AS2_PUBLIC],
        complete=True,
        posted_date=note.published.date() if note.published else None,
        canonical_link=canonical,
        apply_url=ext_apply_url,
        posting_status=ext_posting_status,
        company=company,
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
        # Roll the dedupe window forward — a federated refanout
        # counts as a fresh "seen" event for the local row. Without
        # this, a remote actor still posting an old role does not
        # extend its window and the next non-link/canonical match
        # could re-fork it.
        from job_hunting.models.job_post_dedupe import bump_last_seen
        bump_last_seen(existing)
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
    # Phase 6b — discovery rows for followers of the targeted Company
    # actor (when the inbound Note attributes to a local Company actor
    # URI). The five-clause visibility filter on JobPostViewSet keys
    # off ``discoveries``, so without this step federated rows stay
    # invisible to every local user.
    attributed_to = obj.get("attributedTo") if isinstance(obj, dict) else None
    _create_discoveries_for_company_actor(candidate, attributed_to)
    logger.info(
        "ap.5e.created jp=%s canonical=%s instance=%s activity_id=%s",
        candidate.pk, canonical, instance_host,
        activity.get("id") if isinstance(activity, dict) else None,
    )
    return IngestResult(outcome=OUTCOME_CREATED, job_post=candidate)


def ingest_update_note(
    activity: dict,
    federation_activity: Optional[FederationActivity] = None,
) -> IngestResult:
    """Walk an inbound ``Update(Note)`` against a previously-ingested JP.

    Phase 6b — when the originating peer instance emits an Update for a
    JP we federated in, merge the new field values into our local row
    **but only for fields where the local value is currently empty**.
    Never clobber a local non-empty value: a staff edit, a user
    note-flip, or a more-recent scrape's enrichment must outrank stale
    upstream data.

    Resolution:

    * Object URI → canonicalize → match against ``JobPost.canonical_link``.
    * Sender host (verified actor → urlparse netloc) MUST equal
      ``job_post.source_instance``. Cross-instance updates are silently
      no-ops (the audit row still records the rejected attempt).

    Returns ``IngestResult`` with ``outcome=MERGED`` on a successful
    merge, ``REJECTED`` for a host-mismatch / unknown row / bad shape,
    or ``SKIPPED`` when ingest is kill-switched.
    """
    if not getattr(settings, "ACTIVITYPUB_INGEST_ENABLED", True):
        return IngestResult(outcome=OUTCOME_SKIPPED, reason="ingest_disabled")

    obj = activity.get("object") if isinstance(activity, dict) else None
    if not isinstance(obj, dict):
        return _mark_rejected(federation_activity, "object_not_object")

    obj_type = obj.get("type")
    if obj_type not in INGEST_NOTE_TYPES:
        return IngestResult(outcome=OUTCOME_SKIPPED, reason=f"object_type={obj_type!r}")

    note_url = obj.get("url") or obj.get("id")
    if not isinstance(note_url, str) or not note_url:
        return _mark_rejected(federation_activity, "update_missing_url")
    canonical = canonicalize_link(note_url)
    if not canonical:
        return _mark_rejected(federation_activity, "canonical_link_empty")

    target = (
        JobPost.objects.filter(canonical_link=canonical)
        .order_by("created_at")
        .first()
    )
    if target is None:
        # We don't know this row. Could be an Update for a JP that
        # never reached us (peer's first activity was Update, or our
        # ingest was kill-switched at Create time). Silent no-op.
        return IngestResult(outcome=OUTCOME_SKIPPED, reason="update_target_unknown")

    instance_host = _instance_host(activity)
    if not instance_host:
        return _mark_rejected(federation_activity, "missing_instance_host")
    if instance_host != (target.source_instance or "").lower():
        # Cross-instance authority guard mirrors ``_handle_delete`` — a
        # peer can only update what it originated.
        return _mark_rejected(federation_activity, "update_host_mismatch")

    extension = _extension_payload(obj)
    raw_title = obj.get("name")
    incoming_title = _clip_title(_strip_html(raw_title)) if raw_title else None
    incoming_description = _clip_description(_strip_html(obj.get("content")))
    incoming_apply_url = extension.get("apply_url") if isinstance(extension, dict) else None
    if not isinstance(incoming_apply_url, str) or not incoming_apply_url.strip():
        incoming_apply_url = None
    incoming_posting_status = (
        extension.get("posting_status") if isinstance(extension, dict) else None
    )
    if incoming_posting_status not in {"open", "closed"}:
        incoming_posting_status = None

    # Merge-empty-only: a falsy local value gets overwritten by a
    # non-falsy incoming one; everything else stays put.
    update_fields: list = []
    if not target.title and incoming_title:
        target.title = incoming_title
        update_fields.append("title")
    if not target.description and incoming_description:
        target.description = incoming_description
        update_fields.append("description")
    if not target.apply_url and incoming_apply_url:
        target.apply_url = incoming_apply_url
        update_fields.append("apply_url")
    if not target.posting_status and incoming_posting_status:
        target.posting_status = incoming_posting_status
        update_fields.append("posting_status")

    if update_fields:
        target.save(update_fields=update_fields)
        logger.info(
            "ap.6b.updated jp=%s fields=%s instance=%s",
            target.pk, update_fields, instance_host,
        )

    return IngestResult(outcome=OUTCOME_MERGED, job_post=target)


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
