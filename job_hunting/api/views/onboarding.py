from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from job_hunting.models import (
    CoverLetter,
    JobPost,
    Profile,
    Resume,
    Score,
)


def _reconcile_from_data(user) -> dict:
    """Compute onboarding bits from the user's actual records.

    Keys we cannot infer from data alone (wizard_enabled, resume_reviewed)
    are left for the caller to merge with the stored value.
    """
    return {
        "profile_basics": bool(
            user.first_name and user.last_name and user.email
        ),
        "resume_imported": Resume.objects.filter(user_id=user.id).exists(),
        "first_job_post": JobPost.objects.filter(created_by_id=user.id).exists(),
        "first_score": Score.objects.filter(user_id=user.id).exists(),
        "first_cover_letter": CoverLetter.objects.filter(user_id=user.id).exists(),
    }


@extend_schema(
    tags=["Onboarding"],
    summary="Reconcile the authenticated user's onboarding blob against their real data",
    description=(
        "Inspects the caller's resumes / job posts / scores / cover letters / "
        "profile basics and writes a corrected onboarding JSONB. Preserves the "
        "subjective fields the data cannot answer for — `wizard_enabled` and "
        "`resume_reviewed` — by merging them from the stored value. Returns the "
        "merged blob (same shape as the `onboarding` attribute on the user "
        "resource)."
    ),
    responses={
        200: OpenApiResponse(description="Reconciled onboarding blob"),
        401: OpenApiResponse(description="Authentication required"),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reconcile_onboarding(request):
    user = request.user
    prof, _ = Profile.objects.get_or_create(user_id=user.id)

    derived = _reconcile_from_data(user)
    stored = prof.onboarding if isinstance(prof.onboarding, dict) else {}
    # Preserve subjective/stored-only keys; derived values win on conflict for
    # the keys we can actually verify.
    merged = {**Profile.default_onboarding(), **stored, **derived}
    prof.onboarding = merged
    prof.save(update_fields=["onboarding"])

    return Response({"data": prof.resolved_onboarding(), "meta": {"source": "reconcile"}})
