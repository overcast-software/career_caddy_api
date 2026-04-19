from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from job_hunting.lib.services.application_flow import build_flow
from job_hunting.models import JobPost


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def application_flow_report(request):
    """GET /api/v1/reports/application-flow/?scope=mine|all

    Returns a d3-sankey-shaped payload describing the user's job-post →
    application → status funnel. `scope=all` is staff-only.
    """
    scope = (request.query_params.get("scope") or "mine").lower()
    if scope not in ("mine", "all"):
        scope = "mine"

    if scope == "all":
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Staff only"}]}, status=403
            )
        qs = JobPost.objects.all()
        flow = build_flow(qs, user_id=None)
    else:
        qs = JobPost.objects.filter(
            Q(created_by_id=request.user.id)
            | Q(applications__user_id=request.user.id)
            | Q(scores__user_id=request.user.id)
        ).distinct()
        flow = build_flow(qs, user_id=request.user.id)

    return Response(
        {
            "data": {
                "type": "report",
                "id": "application-flow",
                "attributes": {**flow, "scope": scope},
            }
        }
    )
