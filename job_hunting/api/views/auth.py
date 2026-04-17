import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import get_user_model
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers

from ..serializers import DjangoUserSerializer
from job_hunting.lib.ai_client import set_api_key
from job_hunting.models import Invitation
from ._helpers import _create_user_from_data, _notify_admins_new_signup

logger = logging.getLogger(__name__)


@extend_schema(
    tags=["Auth"],
    summary="Get current user profile",
    responses={
        200: OpenApiResponse(
            description="JSON:API resource object for the authenticated user",
            response=inline_serializer(
                name="ProfileResponse",
                fields={"data": drf_serializers.DictField()},
            ),
        )
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def profile(request):
    """Get the current user's profile information."""
    ser = DjangoUserSerializer()
    resource = ser.to_resource(request.user)
    return Response({"data": resource})


@extend_schema(
    tags=["System"],
    summary="Health check",
    auth=[],
    responses={
        200: OpenApiResponse(
            description="Service is healthy",
            response=inline_serializer(
                name="HealthcheckResponse",
                fields={"healthy": drf_serializers.BooleanField()},
            ),
        )
    },
)
@csrf_exempt
def healthcheck(request):
    """Simple health check endpoint that only reports system health."""
    if request.method == "GET":
        User = get_user_model()
        user_count = User.objects.count()
        return JsonResponse({
            "healthy": True,
            "bootstrap_open": user_count == 0,
            "registration_open": settings.REGISTRATION_OPEN,
        })

    return JsonResponse({"error": "method not allowed"}, status=405)


@csrf_exempt
def guest_session(request):
    """Return a JWT for the guest user — no credentials required."""
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'error': 'method not allowed'}, status=405)

    User = get_user_model()
    try:
        guest = User.objects.select_related('profile_obj').get(username='guest')
        if not guest.profile_obj.is_guest:
            return JsonResponse({'error': 'Guest account not configured.'}, status=400)
    except User.DoesNotExist:
        return JsonResponse({'error': 'Demo mode is not enabled on this server.'}, status=404)

    from rest_framework_simplejwt.tokens import RefreshToken
    refresh = RefreshToken.for_user(guest)
    return JsonResponse({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    })


@extend_schema(
    tags=["System"],
    summary="Check or perform first-time initialization",
    auth=[],
    methods=["GET"],
    responses={
        200: OpenApiResponse(
            description="Initialization status",
            response=inline_serializer(
                name="InitializeStatusResponse",
                fields={
                    "initialization_needed": drf_serializers.BooleanField(),
                    "status": drf_serializers.CharField(),
                },
            ),
        )
    },
)
@extend_schema(
    tags=["System"],
    summary="Create first superuser and configure the application",
    auth=[],
    methods=["POST"],
    request=inline_serializer(
        name="InitializeRequest",
        fields={
            "username": drf_serializers.CharField(required=False),
            "email": drf_serializers.EmailField(required=False),
            "password": drf_serializers.CharField(required=False),
            "first_name": drf_serializers.CharField(required=False),
            "last_name": drf_serializers.CharField(required=False),
            "openai_api_key": drf_serializers.CharField(required=False),
        },
    ),
    responses={
        201: OpenApiResponse(
            description="Initialized successfully",
            response=inline_serializer(
                name="InitializeResponse",
                fields={
                    "initialized": drf_serializers.BooleanField(),
                    "user": drf_serializers.DictField(),
                },
            ),
        ),
        400: OpenApiResponse(description="Bad request / user creation failed"),
        409: OpenApiResponse(description="Already initialized — superuser already exists"),
    },
)
@csrf_exempt
def initialize(request):
    """Initialize the application with first-time setup."""
    if request.method == "GET":
        # Check if initialization is needed
        try:
            User = get_user_model()
            user_count = User.objects.count()
        except Exception:
            # If Django ORM isn't initialized yet, initialization is needed
            user_count = None

        if user_count is None:
            status_str = "unknown"
            initialization_needed = True
        else:
            initialization_needed = user_count == 0
            status_str = (
                "initialized" if not initialization_needed else "needs_initialization"
            )

        return JsonResponse(
            {
                "initialization_needed": initialization_needed,
                "status": status_str,
            }
        )

    if request.method == "POST":
        # Handle initial setup
        try:
            User = get_user_model()
            user_count = User.objects.count()
        except Exception:
            user_count = None

        # Only allow initialization when no users exist
        if user_count is not None and user_count > 0:
            return JsonResponse(
                {"errors": [{"detail": "Application already initialized"}]}, status=409
            )

        # Parse request data
        try:
            import json

            data = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            data = {}

        # Handle both JSON:API and plain JSON formats
        attrs = {}
        if isinstance(data.get("data"), dict):
            attrs = data["data"].get("attributes") or {}
        else:
            attrs = data or {}

        # Extract user creation parameters
        username = attrs.get("username") or attrs.get("name") or "admin"
        email = (attrs.get("email") or None) or None
        password = attrs.get("password") or "admin"
        first_name = attrs.get("first_name") or attrs.get("name") or ""
        last_name = attrs.get("last_name") or ""

        # Extract OpenAI API key
        api_key = (
            attrs.get("openai_api_key")
            or attrs.get("OPENAI_API_KEY")
            or attrs.get("openaiApiKey")
        )

        # Create superuser
        try:
            user = User.objects.create_superuser(
                username=username,
                email=email,
                password=password,
            )
            if first_name:
                user.first_name = first_name
            if last_name:
                user.last_name = last_name
            user.save()
        except Exception as e:
            return JsonResponse(
                {"errors": [{"detail": f"Failed to create user: {str(e)}"}]}, status=400
            )

        # Optionally set OpenAI API key
        meta = {}
        if api_key:
            try:
                set_api_key(str(api_key).strip())
                meta["openai_api_key_saved"] = True
            except Exception as e:
                meta["openai_api_key_saved"] = False
                meta["openai_api_key_error"] = str(e)

        # Return success response
        response_data = {
            "initialized": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        }
        if meta:
            response_data["meta"] = meta

        return JsonResponse(response_data, status=201)

    return JsonResponse({"error": "method not allowed"}, status=405)


@extend_schema(
    methods=["POST"],
    request=inline_serializer(
        name="WaitlistSignup",
        fields={
            "email": drf_serializers.EmailField(),
        },
    ),
    responses={
        201: OpenApiResponse(description="Added to waiting list"),
        400: OpenApiResponse(description="Missing or invalid email"),
        409: OpenApiResponse(description="Email already on the waiting list"),
    },
    auth=[],
)
@csrf_exempt
def waitlist_signup(request):
    """Public endpoint for joining the waiting list."""
    if request.method == "POST":
        import json
        from job_hunting.models import Waitlist

        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            data = {}

        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return JsonResponse(
                {"errors": [{"detail": "A valid email address is required."}]},
                status=400,
            )

        if Waitlist.objects.filter(email=email).exists():
            return JsonResponse(
                {"errors": [{"detail": "This email is already on the waiting list."}]},
                status=409,
            )

        Waitlist.objects.create(email=email)

        try:
            from django.core.mail import send_mail
            from django.template.loader import render_to_string

            body = render_to_string(
                "waitlist_email.txt",
                {"frontend_url": settings.FRONTEND_URL},
            )
            send_mail(
                subject="You're on the Career Caddy waiting list",
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
            )
        except Exception:
            logger.warning("Failed to send waitlist confirmation to %s", email)

        _notify_admins_new_signup(email, email, method="waitlist")

        return JsonResponse({"success": True, "email": email}, status=201)

    return JsonResponse({"error": "method not allowed"}, status=405)


@csrf_exempt
def password_reset_request(request):
    """Request a password-reset email. Always returns 200 to prevent email enumeration."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    import json
    from django.contrib.auth.tokens import PasswordResetTokenGenerator
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JsonResponse(
            {"errors": [{"detail": "A valid email address is required."}]},
            status=400,
        )

    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()

    if user:
        token_generator = PasswordResetTokenGenerator()
        token = token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}&uid={uid}"

        body = render_to_string(
            "password_reset_email.txt", {"reset_url": reset_url}
        )
        send_mail(
            subject="Password Reset — Career Caddy",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
        )

    return JsonResponse(
        {
            "message": (
                "If an account with that email exists, "
                "a password reset link has been sent."
            )
        },
        status=200,
    )


@csrf_exempt
def password_reset_confirm(request):
    """Confirm a password reset with token, uid, and new password."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    import json
    from django.contrib.auth.tokens import PasswordResetTokenGenerator
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    from django.utils.http import urlsafe_base64_decode
    from django.utils.encoding import force_str

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    token = data.get("token", "")
    uid = data.get("uid", "")
    new_password = data.get("new_password", "")

    if not token or not uid or not new_password:
        return JsonResponse(
            {"errors": [{"detail": "token, uid, and new_password are required."}]},
            status=400,
        )

    User = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return JsonResponse(
            {"errors": [{"detail": "Invalid or expired reset link."}]},
            status=400,
        )

    token_generator = PasswordResetTokenGenerator()
    if not token_generator.check_token(user, token):
        return JsonResponse(
            {"errors": [{"detail": "Invalid or expired reset link."}]},
            status=400,
        )

    try:
        validate_password(new_password, user)
    except ValidationError as e:
        return JsonResponse(
            {"errors": [{"detail": msg} for msg in e.messages]},
            status=400,
        )

    user.set_password(new_password)
    user.save()

    return JsonResponse(
        {"message": "Password has been reset successfully."}, status=200
    )


@csrf_exempt
def accept_invite(request):
    """Accept an invitation and create an account."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    import json
    from django.utils import timezone as tz
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    token = (data.get("token") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()

    if not token:
        return JsonResponse(
            {"errors": [{"detail": "Invitation token is required."}]}, status=400
        )

    invitation = Invitation.objects.filter(token=token).first()
    if not invitation or not invitation.is_valid:
        return JsonResponse(
            {"errors": [{"detail": "Invalid or expired invitation link."}]},
            status=400,
        )

    user, errors, error_status = _create_user_from_data(
        username=username,
        password=password,
        email=invitation.email,
        first_name=first_name,
        last_name=last_name,
    )
    if errors:
        return JsonResponse({"errors": errors}, status=error_status)

    invitation.accepted_at = tz.now()
    invitation.save()

    # Send welcome email
    try:
        login_url = f"{settings.FRONTEND_URL}/login"
        body = render_to_string(
            "welcome_email.txt",
            {"username": username, "login_url": login_url},
        )
        send_mail(
            subject="Welcome to Career Caddy",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invitation.email],
        )
    except Exception:
        logger.warning("Failed to send welcome email to %s", invitation.email)

    ser = DjangoUserSerializer()
    return JsonResponse({"data": ser.to_resource(user)}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdminUser])
def test_email(request):
    """Admin-only endpoint to verify email delivery."""
    from django.utils import timezone as tz
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    data = request.data or {}

    target_email = (data.get("email") or "").strip().lower()
    if not target_email:
        target_email = request.user.email

    if not target_email or "@" not in target_email:
        return Response(
            {"errors": [{"detail": "No valid email address available."}]},
            status=400,
        )

    body = render_to_string(
        "test_email.txt", {"timestamp": tz.now().isoformat()}
    )
    send_mail(
        subject="Test Email — Career Caddy",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[target_email],
    )

    return Response(
        {"message": f"Test email sent to {target_email}."}, status=200
    )
