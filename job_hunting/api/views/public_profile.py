"""Public, no-auth profile read — a user's federated (published) job posts.

Powers the public ``/<username>`` (e.g. ``/dough``) web profile: the
human-readable twin of the ActivityPub actor outbox. CC #51 / PACA.

Single public surface contract:
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
    """Build the public JSON:API ``public-job-post`` resource for ``job_post``.

    Emits the distinct ``type: "public-job-post"`` — this projection is a
    different resource than the authed ``job-post`` (different field set,
    different permissions), so it carries its own type rather than
    masquerading as the full resource. It emits only the public-safe
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
        "type": "public-job-post",
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
        200: OpenApiResponse(description="JSON:API list of public-job-post resources")
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
