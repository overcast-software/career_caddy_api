"""Public, no-auth profile read — a user and their federated job posts.

Powers the public ``/<username>`` (e.g. ``/dough``) web profile: the
human-readable twin of the ActivityPub actor outbox. CC #51 / PACA.

Two public surfaces, both ``AllowAny``:

- ``GET /api/v1/users/<username>/`` — the profile OWNER as a normal
  JSON:API ``user`` resource (canonical numeric id, public-safe attrs
  only) exposing a ``federated`` relationship. The relationship is
  LINK-ONLY (``links.related`` points at the collection below); there
  is deliberately no ``?include=federated`` sideload — the SPA paginates
  the feed via the collection query instead, so sideloading the whole
  first page onto the user resource is dead surface.
- ``GET /api/v1/users/<username>/job-posts/federated/`` — the federated
  (published) job posts themselves, as normal ``job-post`` resources (a
  public-safe SUBSET of the fields, NOT a shadow ``public-job-post``
  type). Both surfaces share one "published" filter so the web profile
  and the fediverse outbox can never disagree.

Federated-collection contract:
- Route: ``GET /api/v1/users/<username>/job-posts/federated/`` (trailing
  slash optional, per the router's dual-slash convention).
- Permission: ``AllowAny`` — never requires login. This is the ONLY
  publicly-routed view of a user's posts; the bare
  ``/users/<username>/job-posts/`` is deliberately NOT a public route, so
  there is no drop-the-filter leak path.
- Source of truth for "published": reuses
  :func:`...federation.public_jobpost_queryset_for_user`, the exact
  filter the AP outbox uses (``created_by`` == user AND ``audience``
  contains ``AS2_PUBLIC``, ``order_by(-created_at, -id)``). The web
  profile and the fediverse view can never disagree on what's public.
- Pagination: KEYSET (cursor) on the composite ``(-created_at, -id)``
  order — the Mastodon / ActivityPub-outbox pattern. A growing feed
  (new federated posts arriving while the visitor scrolls) never dupes
  or skips an already-seen row, which offset/page-number paging cannot
  promise. See :func:`public_user_federated_job_posts`.
- Payload: a deliberate PUBLIC PROJECTION (``_public_jobpost_resource``)
  carrying only display fields. It does NOT reuse ``JobPostSerializer``,
  which over-shares (per-user scores, every user's applications /
  cover-letters / questions linkage, triage meta, ``source``,
  ``audience``, dedupe-pipeline columns). Vetting exposure is a separate
  later epic (CC-52) and is intentionally out of scope here.

Never returns private / NULL-audience posts; never returns another
user's posts. Unknown username → 404. Known user with no public posts →
200 with an empty list.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db.models import Q
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from drf_spectacular.types import OpenApiTypes
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from job_hunting.api.serializers import _to_primitive
from job_hunting.api.views.federation import public_jobpost_queryset_for_user
from job_hunting.lib.as_object import (
    resolve_personal_annotations_batch,
    user_opted_into_rich,
)


# Deliberate public projection. Display-only fields a visitor needs to
# read a published posting — mirrors the public AS2 outbox Note's
# field set (lib/as_object.job_post_as_object). Anything per-user,
# operator-facing, or dedupe-pipeline internal is intentionally absent:
# NO source, audience, canonical_link / fingerprints, duplicate_of_id,
# reposted_from_id, complete, extraction_date, apply_url_status,
# apply_url_resolved_at, source_instance, source_deleted_at, top_score,
# and NO scores / applications / cover-letters / questions / discoveries
# relationships.
_PUBLIC_ATTRS = (
    "title",
    "description",
    "link",
    "apply_url",
    "location",
    "remote",
    "salary_min",
    "salary_max",
    "posted_date",
    "created_at",
    "posting_status",
)


def _public_jobpost_resource(job_post, annotations=None) -> dict:
    """Build the public JSON:API ``job-post`` resource for ``job_post``.

    Emits the NORMAL ``type: "job-post"`` — this is a PARTIAL payload of
    the one ``job-post`` resource (a public-safe field SUBSET), not a
    shadow type. The frontend uses the single ``job-post`` model, so the
    public projection collapses onto the same record rather than minting a
    parallel ``public-job-post`` type. It emits only the public-safe
    attribute subset plus a denormalized ``company_name`` (Company is a
    shared resource and there is no public Company read endpoint, so the
    name is inlined rather than linked).

    ``annotations`` (BACK-103 / CC-104) is a ``PersonalAnnotations`` and is
    supplied when the profile owner has ``federate_rich=True`` (publicly, to
    ALL visitors) OR when the requester is the authenticated owner viewing
    their own page — it surfaces the owner's verdict / score / applied under
    ``meta.federation`` so the rich ``/@dough`` page can render its show-off
    line. When ``federate_rich`` is off, the anonymous/non-owner projection
    passes None and stays public-safe (no private vetting leak).
    """
    attrs = {name: _to_primitive(getattr(job_post, name)) for name in _PUBLIC_ATTRS}
    company_name = None
    if job_post.company_id:
        company = job_post.company
        company_name = getattr(company, "name", None) or getattr(
            company, "display_name", None
        )
    attrs["company_name"] = company_name
    resource = {
        "type": "job-post",
        "id": str(job_post.id),
        "attributes": attrs,
    }
    if annotations is not None:
        resource["meta"] = {
            "federation": {
                "verdict": annotations.verdict,
                "verdict_reason_code": annotations.reason_code,
                "score": annotations.score,
                "applied": annotations.applied,
            }
        }
    return resource


# Keyset-pagination bounds for the federated feed. Defaults sized for an
# infinite-scroll viewport; capped so a hostile ``page[size]`` can't pull
# the whole table in one request.
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


class _InvalidCursor(Exception):
    """Raised when ``?page[after]`` can't be decoded into ``(created_at, id)``."""


def _keyset_page_size(request) -> int:
    """Parse ``?page[size]=N`` for the keyset feed (default 20, cap 100)."""
    qp = request.query_params
    try:
        size = int(qp.get("page[size]") or qp.get("per_page") or _DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = _DEFAULT_PAGE_SIZE
    return max(1, min(size, _MAX_PAGE_SIZE))


def _encode_cursor(job_post) -> str:
    """Opaque, URL-safe cursor for ``job_post``'s ``(created_at, id)`` key.

    base64 of ``"<iso_created_at>|<id>"``. Opaque on purpose — clients
    must treat it as a token to echo back via ``page[after]`` (or follow
    ``links.next``), not parse. ISO format round-trips the timestamp at
    microsecond precision so the keyset comparison is exact.
    """
    raw = f"{job_post.created_at.isoformat()}|{job_post.id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode an opaque cursor into ``(created_at, id)``.

    Raises :class:`_InvalidCursor` on ANY malformed input (bad base64,
    bad utf-8, missing delimiter, unparseable timestamp) so the view
    can answer a clean 400 rather than 500'ing on a hand-edited or
    truncated cursor. The id is a NanoID string PK (CC-77) — kept
    verbatim, never coerced to int; the keyset comparison against the
    varchar PK is lexical, consistent with the ``-id`` DB ordering.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        iso, sep, raw_id = raw.rpartition("|")
        if not sep or not iso or not raw_id:
            raise ValueError("cursor missing '<iso>|<id>' delimiter")
        return datetime.fromisoformat(iso), raw_id
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise _InvalidCursor(str(exc)) from exc


@extend_schema(
    tags=["Job Posts"],
    summary="Public list of a user's federated (published) job posts",
    description=(
        "Public, no-auth read of the posts a user has published "
        "(audience-public) — the human-readable twin of the ActivityPub "
        "actor outbox. KEYSET (cursor) paginated on ``(-created_at, -id)`` "
        "so the feed is stable while new federated posts arrive: pass "
        "``page[size]`` (default 20, max 100) and ``page[after]`` (the "
        "opaque cursor from a previous page's ``meta.next_cursor`` / "
        "``links.next``). Never returns private / NULL-audience posts, "
        "never another user's posts, never requires login. Unknown "
        "username → 404; known user with no public posts → 200 with an "
        "empty list; malformed cursor → 400."
    ),
    parameters=[
        OpenApiParameter(
            "username",
            OpenApiTypes.STR,
            OpenApiParameter.PATH,
            description="Username (not id) of the profile owner",
        ),
        OpenApiParameter(
            "page[size]",
            OpenApiTypes.INT,
            OpenApiParameter.QUERY,
            description="Page size (default 20, capped at 100)",
        ),
        OpenApiParameter(
            "page[after]",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            description="Opaque keyset cursor — echo meta.next_cursor to page forward",
        ),
    ],
    responses={
        200: OpenApiResponse(description="JSON:API list of job-post resources"),
        400: OpenApiResponse(description="Malformed pagination cursor"),
        404: OpenApiResponse(description="Unknown username"),
    },
)
@api_view(["GET"])
@permission_classes([AllowAny])
def public_user_federated_job_posts(request, username):
    """GET /api/v1/users/<username>/job-posts/federated/ — see module docstring.

    Keyset paging: order is ``(-created_at, -id)``; a cursor encodes the
    last row's ``(created_at, id)`` and the next slice is everything
    strictly "older" in that composite order
    (``created_at < c`` OR ``created_at == c AND id < c_id``). One extra
    row is fetched per page to detect whether a further page exists
    without paying for a separate ``COUNT(*)``.
    """
    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is None:
        return Response(
            {"errors": [{"status": "404", "detail": "User not found"}]},
            status=404,
        )

    qs = public_jobpost_queryset_for_user(user.id).select_related("company")
    page_size = _keyset_page_size(request)

    after = request.query_params.get("page[after]") or request.query_params.get(
        "page_after"
    )
    if after:
        try:
            cursor_created_at, cursor_id = _decode_cursor(after)
        except _InvalidCursor:
            return Response(
                {
                    "errors": [
                        {"status": "400", "detail": "Malformed pagination cursor"}
                    ]
                },
                status=400,
            )
        # Descending keyset: rows strictly "after" the cursor in
        # (-created_at, -id) order are the ones older than it.
        qs = qs.filter(
            Q(created_at__lt=cursor_created_at)
            | Q(created_at=cursor_created_at, id__lt=cursor_id)
        )

    # Over-fetch by one to learn whether another page exists — cheaper
    # than a second COUNT against a growing feed.
    window = list(qs[: page_size + 1])
    has_more = len(window) > page_size
    items = window[:page_size]

    next_cursor = _encode_cursor(items[-1]) if (has_more and items) else None

    self_url = request.build_absolute_uri(request.path)
    links = {"self": request.build_absolute_uri()}
    if next_cursor is not None:
        links["next"] = f"{self_url}?page[size]={page_size}&page[after]={next_cursor}"

    # CC-104: when the profile owner has opted into rich federation
    # (``Profile.federate_rich``), enrich each published post with the
    # owner's verdict / score / applied under ``meta.federation`` for ALL
    # visitors (anonymous included) — the web /@dough page renders rich,
    # consistent with the fediverse Note that already publishes these same
    # signals publicly (``as_object._should_render_rich``). The ``is_owner``
    # leg stays as an OR so the owner keeps their own preview even before
    # opting in (the SPA sends its JWT even on this AllowAny route). When
    # ``federate_rich`` is off, anonymous + non-owner visitors get the
    # public-safe projection only — another user's private vetting never
    # leaks.
    viewer = getattr(request, "user", None)
    is_owner = bool(
        viewer is not None
        and getattr(viewer, "is_authenticated", False)
        and viewer.id == user.id
    )
    expose_federation = is_owner or user_opted_into_rich(user.id)
    annotation_map = (
        resolve_personal_annotations_batch(items, user.id)
        if expose_federation
        else {}
    )
    payload = {
        "data": [
            _public_jobpost_resource(jp, annotations=annotation_map.get(jp.pk))
            for jp in items
        ],
        "links": links,
        "meta": {"next_cursor": next_cursor},
    }
    return Response(payload)


def _public_user_resource(user) -> dict:
    """Build the public JSON:API ``user`` resource for ``user``.

    Emits the NORMAL ``user`` type carrying the user's CANONICAL numeric
    id (string form) — NOT the username. The username is only the URL
    lookup key; keeping the resource id as the canonical id means the
    public resource collapses onto the same record the authed
    ``/users/<id>/`` + ``/me/`` endpoints return in the Ember store, so no
    duplicate-user record is created.

    Attributes are PUBLIC-SAFE ONLY: ``username`` plus a derived
    ``display_name`` (``first_name last_name`` trimmed, falling back to
    the username). Email, is_staff, is_active, phone, address, and every
    other private / PII field on the User+Profile pair are intentionally
    absent — there is no public Company-style read endpoint that would
    justify leaking them.
    """
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    display_name = full_name or user.username
    return {
        "type": "user",
        "id": str(user.id),
        "attributes": {
            "username": user.username,
            "display_name": display_name,
        },
    }


@extend_schema(
    tags=["Users"],
    summary="Public profile of a user (with a federated job-posts relationship)",
    description=(
        "Public, no-auth read of a single user as a JSON:API ``user`` "
        "resource — the owner of the public profile page. Carries the "
        "user's canonical numeric id (not the username) and PUBLIC-SAFE "
        "attributes only (username, display_name); never email, is_staff, "
        "phone, or other PII. The ``federated`` relationship is LINK-ONLY: "
        "``links.related`` points at the keyset-paginated federated "
        "job-posts collection, which the client fetches and scrolls "
        "directly. There is no ``?include=federated`` sideload. Unknown "
        "username → 404."
    ),
    parameters=[
        OpenApiParameter(
            "username",
            OpenApiTypes.STR,
            OpenApiParameter.PATH,
            description="Username (not id) of the profile owner",
        ),
    ],
    responses={
        200: OpenApiResponse(description="JSON:API user resource"),
        404: OpenApiResponse(description="Unknown username"),
    },
)
@api_view(["GET"])
@permission_classes([AllowAny])
def public_user_profile(request, username):
    """GET /api/v1/users/<username>/ — public ``user`` resource.

    The ``federated`` relationship emits a link-only ``links.related``
    pointing at the public federated-job-posts collection
    (:func:`public_user_federated_job_posts`); the client paginates that
    collection itself rather than sideloading it here. Unknown username
    → 404.
    """
    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is None:
        return Response(
            {"errors": [{"status": "404", "detail": "User not found"}]},
            status=404,
        )

    resource = _public_user_resource(user)
    related = f"/api/v1/users/{user.username}/job-posts/federated/"
    resource["relationships"] = {"federated": {"links": {"related": related}}}
    return Response({"data": resource})
