"""Public, no-auth profile read — a user and their federated job posts.

Powers the public ``/<username>`` (e.g. ``/dough``) web profile: the
human-readable twin of the ActivityPub actor outbox. CC #51 / PACA.

Two public surfaces, both ``AllowAny``:

- ``GET /api/v1/users/<username>/`` — the profile OWNER as a normal
  JSON:API ``user`` resource (canonical numeric id, public-safe attrs
  only) exposing a ``federated`` relationship. With ``?include=federated``
  it sideloads that page of public ``job-post`` resources.
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

import math

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from drf_spectacular.types import OpenApiTypes
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from job_hunting.api.serializers import _to_primitive
from job_hunting.api.views.federation import public_jobpost_queryset_for_user


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


def _public_jobpost_resource(job_post) -> dict:
    """Build the public JSON:API ``job-post`` resource for ``job_post``.

    Emits the NORMAL ``type: "job-post"`` — this is a PARTIAL payload of
    the one ``job-post`` resource (a public-safe field SUBSET), not a
    shadow type. The frontend uses the single ``job-post`` model, so the
    public projection collapses onto the same record rather than minting a
    parallel ``public-job-post`` type. It emits only the public-safe
    attribute subset plus a denormalized ``company_name`` (Company is a
    shared resource and there is no public Company read endpoint, so the
    name is inlined rather than linked).
    """
    attrs = {name: _to_primitive(getattr(job_post, name)) for name in _PUBLIC_ATTRS}
    company_name = None
    if job_post.company_id:
        company = job_post.company
        company_name = getattr(company, "name", None) or getattr(
            company, "display_name", None
        )
    attrs["company_name"] = company_name
    return {
        "type": "job-post",
        "id": str(job_post.id),
        "attributes": attrs,
    }


def _page_params(request):
    """Parse (page, per_page) supporting JSON:API + simple styles.

    Mirrors BaseViewSet._page_params bounds (per_page clamped 1..200,
    default 50) so the public endpoint paginates like the rest of the
    API.
    """
    qp = request.query_params
    try:
        page = int(qp.get("page[number]") or qp.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(qp.get("page[size]") or qp.get("per_page") or 50)
    except (TypeError, ValueError):
        per_page = 50
    return max(1, page), max(1, min(per_page, 200))


@extend_schema(
    tags=["Job Posts"],
    summary="Public list of a user's federated (published) job posts",
    description=(
        "Public, no-auth read of the posts a user has published "
        "(audience-public) — the human-readable twin of the ActivityPub "
        "actor outbox. Never returns private / NULL-audience posts, never "
        "another user's posts, never requires login. Unknown username "
        "→ 404; known user with no public posts → 200 with an empty list."
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
        200: OpenApiResponse(description="JSON:API list of job-post resources")
    },
)
@api_view(["GET"])
@permission_classes([AllowAny])
def public_user_federated_job_posts(request, username):
    """GET /api/v1/users/<username>/job-posts/federated/ — see module docstring."""
    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is None:
        return Response(
            {"errors": [{"status": "404", "detail": "User not found"}]},
            status=404,
        )

    qs = public_jobpost_queryset_for_user(user.id).select_related("company")
    total = qs.count()
    page, per_page = _page_params(request)
    total_pages = math.ceil(total / per_page) if per_page else 1
    offset = (page - 1) * per_page
    items = list(qs[offset : offset + per_page])

    payload = {
        "data": [_public_jobpost_resource(jp) for jp in items],
        "meta": {
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        },
    }
    if page < total_pages:
        base = request.build_absolute_uri(request.path)
        payload["links"] = {"next": f"{base}?page={page + 1}&per_page={per_page}"}
    else:
        payload["links"] = {"next": None}
    return Response(payload)


def _federated_include_requested(request) -> bool:
    """True when the caller asked to sideload the ``federated`` relationship.

    Tolerates both ``?include=`` and ``?includes=`` (the codebase accepts
    either) and comma-separated lists, so ``?include=federated`` and
    ``?include=federated,other`` both trigger the compound document.
    """
    for key in ("include", "includes"):
        raw = request.query_params.get(key)
        if raw:
            tokens = {s.strip() for s in str(raw).split(",") if s.strip()}
            if "federated" in tokens:
                return True
    return False


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
        "phone, or other PII. The ``federated`` relationship always links "
        "to the user's published job posts; pass ``?include=federated`` to "
        "additionally sideload that page of public ``job-post`` resources "
        "as ``included``. Unknown username → 404."
    ),
    parameters=[
        OpenApiParameter(
            "username",
            OpenApiTypes.STR,
            OpenApiParameter.PATH,
            description="Username (not id) of the profile owner",
        ),
        OpenApiParameter(
            "include",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            description="Set to 'federated' to sideload the federated job posts",
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

    The ``federated`` relationship always emits a ``links.related``
    pointing at the public federated-job-posts collection. With
    ``?include=federated`` it ALSO emits ``relationships.federated.data``
    linkage for the current page plus a top-level ``included`` array of
    the public ``job-post`` resources (same rows as
    :func:`public_user_federated_job_posts`, via
    :func:`public_jobpost_queryset_for_user`). Unknown username → 404.
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
    federated = {"links": {"related": related}}
    payload = {"data": resource}

    if _federated_include_requested(request):
        qs = public_jobpost_queryset_for_user(user.id).select_related("company")
        page, per_page = _page_params(request)
        offset = (page - 1) * per_page
        items = list(qs[offset : offset + per_page])
        federated["data"] = [
            {"type": "job-post", "id": str(jp.id)} for jp in items
        ]
        payload["included"] = [_public_jobpost_resource(jp) for jp in items]

    resource["relationships"] = {"federated": federated}
    return Response(payload)
