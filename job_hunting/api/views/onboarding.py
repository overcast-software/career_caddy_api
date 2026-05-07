from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from job_hunting.models import Profile

User = get_user_model()


def _resolve_target_user(request, user_id_or_me):
    """Resolve `me` to request.user.id, validate the caller may access the
    target user. Returns (target_id, error_response_or_None).

    Non-staff callers can only access their own onboarding. Staff can read
    anyone's. The `me` alias means clients (frontend, agent, MCP) don't need
    to look up their own id before calling.

    Existence is enforced downstream by the FK on Profile.user — checking
    here would only open a TOCTOU window where the user could be deleted
    between the check and the get_or_create.
    """
    if user_id_or_me == "me":
        return request.user.id, None
    try:
        target_id = int(user_id_or_me)
    except (TypeError, ValueError):
        return None, Response(
            {"errors": [{"detail": "Invalid user id."}]}, status=400
        )
    if target_id != request.user.id and not request.user.is_staff:
        return None, Response(
            {"errors": [{"detail": "Forbidden."}]}, status=403
        )
    return target_id, None


def _get_or_create_profile(target_id):
    """Atomically fetch-or-create the profile, surfacing FK-violations as
    404 when the user was deleted between auth and this call."""
    try:
        with transaction.atomic():
            prof, _ = Profile.objects.get_or_create(user_id=target_id)
            return prof, None
    except IntegrityError:
        return None, Response(
            {"errors": [{"detail": "User not found."}]}, status=404
        )


@extend_schema(
    tags=["Onboarding"],
    summary="Read or update the authenticated user's onboarding state",
    description=(
        "GET: read-only fetch of the resolved onboarding blob, partitioned "
        "into `derived` (recomputed by reconcile from real data) and "
        "`subjective` (user/AW writable). Use for polling from the wizard. "
        "PATCH: strict subjective-only update. Accepts a flat dict of "
        "subjective keys (`wizard_enabled`, `resume_reviewed`); 400 on any "
        "derived or unknown key. Use POST /api/v1/users/me/onboarding/reconcile/ "
        "when you explicitly need to refresh the derived flags from current "
        "records."
    ),
    responses={
        200: OpenApiResponse(description="Resolved onboarding blob, split shape"),
        400: OpenApiResponse(description="PATCH contained derived or unknown keys"),
        401: OpenApiResponse(description="Authentication required"),
    },
)
@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def onboarding_endpoint(request, user_id):
    target_id, err = _resolve_target_user(request, user_id)
    if err is not None:
        return err
    prof, err = _get_or_create_profile(target_id)
    if err is not None:
        return err

    if request.method == "PATCH":
        # Only the user themselves can mutate their onboarding; staff can
        # read but should not silently flip another user's wizard state.
        if target_id != request.user.id:
            return Response(
                {"errors": [{"detail": "Cannot mutate another user's onboarding."}]},
                status=403,
            )
        patch = request.data if isinstance(request.data, dict) else {}
        # Accept three input shapes so both Ember Data clients and
        # agent tools can write without a serializer in between:
        #   1. flat:                     {"wizard_enabled": false}
        #   2. JSON:API attributes:      {"data": {"attributes": {...}}}
        #   3. nested subjective half:   {"subjective": {...}}  (also the
        #      shape Ember Data sends since the model attribute is named
        #      `subjective` and changed attrs land as attributes.subjective).
        if "data" in patch and isinstance(patch["data"], dict):
            # Defense-in-depth: if the JSON:API resource body carries an
            # `id`, it MUST match the URL target. Disagreement means the
            # body and URL describe different users — refuse rather than
            # silently apply to the URL target. Catches confused clients
            # and would-be MITM swaps that rewrote one but not the other.
            body_id = patch["data"].get("id")
            if body_id is not None and str(body_id) != str(target_id):
                return Response(
                    {
                        "errors": [
                            {
                                "detail": (
                                    "Onboarding PATCH body id "
                                    f"({body_id}) does not match URL target "
                                    f"({target_id})."
                                ),
                            }
                        ]
                    },
                    status=409,
                )
            patch = patch["data"].get("attributes") or {}
        if "subjective" in patch and isinstance(patch["subjective"], dict):
            patch = patch["subjective"]
        rejected = prof.merge_subjective_onboarding(patch)
        if rejected:
            return Response(
                {
                    "errors": [
                        {
                            "detail": (
                                "Onboarding PATCH may only set subjective keys "
                                f"({sorted(Profile.SUBJECTIVE_ONBOARDING_KEYS)}). "
                                f"Rejected: {sorted(rejected)}. Derived keys "
                                "are recomputed by /api/v1/users/me/onboarding/reconcile/."
                            ),
                        }
                    ]
                },
                status=400,
            )
        prof.save(update_fields=["onboarding"])

    return Response(_onboarding_resource(target_id, prof, source="stored"))


def _onboarding_resource(user_id: int, prof: Profile, *, source: str) -> dict:
    """Wrap the resolved onboarding blob in a JSON:API resource document.

    The onboarding endpoint is a singleton per user — id matches the
    authenticated user's id so Ember Data can identify the record.
    """
    return {
        "data": {
            "type": "onboarding",
            "id": str(user_id),
            "attributes": Profile.split_onboarding(prof.resolved_onboarding()),
        },
        "meta": {"source": source},
    }


@extend_schema(
    tags=["Onboarding"],
    summary="Reconcile the authenticated user's onboarding blob against their real data",
    description=(
        "Inspects the caller's resumes / job posts / scores / cover letters / "
        "profile basics and writes a corrected onboarding JSONB. Preserves the "
        "subjective fields the data cannot answer for — `wizard_enabled` and "
        "`resume_reviewed` — by merging them from the stored value. Returns "
        "the resolved blob in the same `{derived, subjective}` split shape as "
        "GET /api/v1/onboarding/."
    ),
    responses={
        200: OpenApiResponse(description="Reconciled onboarding blob, split shape"),
        401: OpenApiResponse(description="Authentication required"),
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reconcile_onboarding(request, user_id):
    target_id, err = _resolve_target_user(request, user_id)
    if err is not None:
        return err
    # Reconcile mutates derived flags from real data — only the user
    # themselves should trigger that on their own row. Staff can observe
    # but shouldn't kick a reconcile on another user's behalf.
    if target_id != request.user.id:
        return Response(
            {"errors": [{"detail": "Cannot reconcile another user's onboarding."}]},
            status=403,
        )
    prof, err = _get_or_create_profile(target_id)
    if err is not None:
        return err

    derived = prof.derive_onboarding_from_state()
    stored = prof.onboarding if isinstance(prof.onboarding, dict) else {}
    # Preserve subjective/stored-only keys; derived values win on conflict for
    # the keys we can actually verify.
    merged = {**Profile.default_onboarding(), **stored, **derived}
    prof.onboarding = merged
    prof.save(update_fields=["onboarding"])

    return Response(_onboarding_resource(target_id, prof, source="reconcile"))
