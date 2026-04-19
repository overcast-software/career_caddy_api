from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.db.models.functions import Length
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from job_hunting.lib.services.application_flow import build_flow
from job_hunting.lib.services.source_flow import build_sources
from job_hunting.models import JobPost


# Canonical list of JobPost.source values we know about. Kept here so the
# frontend dropdown (and sankey filter) always offers the same options even
# on a database that hasn't yet seen all of them. Callers merge this list
# with distinct DB values so a new origin appears the moment it's used.
KNOWN_SOURCES = ["manual", "email", "paste", "scrape", "chat", "import"]


def _user_scoped_job_posts(user_id):
    return JobPost.objects.filter(
        Q(created_by_id=user_id)
        | Q(applications__user_id=user_id)
        | Q(scores__user_id=user_id)
    ).distinct()


def _parse_iso_date(val):
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except (TypeError, ValueError):
        return None


def _apply_report_filters(qs, request):
    """Layer source / date-range / person filters on top of a JobPost qs.

    Returns (qs, err_response). err_response is non-None when the caller
    should short-circuit (403 for non-staff trying ?user=).
    """
    params = request.query_params

    source = (params.get("source") or "").strip()
    if source and source != "all":
        qs = qs.filter(source=source)

    date_from = _parse_iso_date(params.get("from"))
    date_to = _parse_iso_date(params.get("to"))
    if date_from:
        qs = qs.filter(created_at__gte=date_from)
    if date_to:
        # Make the right bound inclusive of the whole day.
        qs = qs.filter(created_at__lt=date_to + timedelta(days=1))

    # Char-length approximation of STUB_MIN_WORDS=60 word threshold. Same
    # shape as filter[stub] on JobPostViewSet.list.
    if str(params.get("exclude_stubs", "")).lower() in ("1", "true", "yes"):
        qs = qs.annotate(_desc_len=Length("description")).exclude(
            Q(description__isnull=True) | Q(description="") | Q(_desc_len__lt=450)
        )

    return qs, None


def _person_filter_effective_user_id(request):
    """Handle the ?user= staff-only filter. Returns (user_id, err_response).

    For staff: returns the requested user id (or None if not specified).
    For non-staff with ?user= set: returns a 403 response to short-circuit
    the view.
    """
    raw = request.query_params.get("user")
    if not raw:
        return None, None
    if not request.user.is_staff:
        return None, Response(
            {"errors": [{"detail": "Staff only for ?user= filter"}]}, status=403
        )
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, Response(
            {"errors": [{"detail": "Invalid ?user= value"}]}, status=400
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def application_flow_report(request):
    """GET /api/v1/reports/application-flow/?scope=mine|all&source=&from=&to=&user=

    Sankey funnel payload. Filters: source (provenance), date range
    (JobPost.created_at), user (staff-only, scopes to that person).

    Public: anonymous viewers get the global aggregate (scope=all,
    user_id=None) — the funnel is marketing-visible; clickthroughs land
    on auth-guarded pages so no per-user data leaks.
    """
    is_authed = request.user and request.user.is_authenticated
    scope = (request.query_params.get("scope") or "mine").lower()
    if scope not in ("mine", "all"):
        scope = "mine"

    if not is_authed:
        scope = "all"
        qs = JobPost.objects.all()
        effective_user_id = None
    else:
        person_user_id, err = _person_filter_effective_user_id(request)
        if err:
            return err

        if scope == "all":
            if not request.user.is_staff:
                return Response(
                    {"errors": [{"detail": "Staff only"}]}, status=403
                )
            if person_user_id is not None:
                qs = _user_scoped_job_posts(person_user_id)
                effective_user_id = person_user_id
            else:
                qs = JobPost.objects.all()
                effective_user_id = None
        else:
            qs = _user_scoped_job_posts(request.user.id)
            effective_user_id = request.user.id

    qs, err = _apply_report_filters(qs, request)
    if err:
        return err

    flow = build_flow(qs, user_id=effective_user_id)

    return Response(
        {
            "data": {
                "type": "report",
                "id": "application-flow",
                "attributes": {**flow, "scope": scope},
            }
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sources_report(request):
    """GET /api/v1/reports/sources/?scope=mine|all&source=&from=&to=&user=

    Horizontal stacked bar payload. Same filter set as application-flow.
    """
    scope = (request.query_params.get("scope") or "mine").lower()
    if scope not in ("mine", "all"):
        scope = "mine"

    person_user_id, err = _person_filter_effective_user_id(request)
    if err:
        return err

    if scope == "all":
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Staff only"}]}, status=403
            )
        if person_user_id is not None:
            qs = _user_scoped_job_posts(person_user_id)
            effective_user_id = person_user_id
        else:
            qs = JobPost.objects.all()
            effective_user_id = None
    else:
        qs = _user_scoped_job_posts(request.user.id)
        effective_user_id = request.user.id

    qs, err = _apply_report_filters(qs, request)
    if err:
        return err

    rollup = build_sources(qs, user_id=effective_user_id)

    return Response(
        {
            "data": {
                "type": "report",
                "id": "sources",
                "attributes": {**rollup, "scope": scope},
            }
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_filter_options(request):
    """GET /api/v1/reports/filter-options/

    Populates the frontend FilterBar: available sources (KNOWN_SOURCES
    union distinct DB values so a new origin appears immediately) and,
    staff-only, the list of users for the person filter.
    """
    db_sources = set(
        JobPost.objects.order_by()
        .values_list("source", flat=True)
        .distinct()
    )
    # Preserve the canonical order, then append any DB-only extras sorted.
    known = [s for s in KNOWN_SOURCES if s in db_sources or s in KNOWN_SOURCES]
    extras = sorted(db_sources - set(KNOWN_SOURCES))
    sources = known + extras

    payload = {"sources": sources}

    if request.user.is_staff:
        User = get_user_model()
        users = []
        for u in User.objects.order_by("username").values(
            "id", "username", "first_name", "last_name"
        ):
            first = u.get("first_name") or ""
            last = u.get("last_name") or ""
            full = f"{first} {last}".strip()
            display = f"{u['username']} ({full})" if full else u["username"]
            users.append({"id": u["id"], "display": display})
        payload["users"] = users

    return Response(payload)
