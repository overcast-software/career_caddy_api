import logging

from django.contrib.auth import get_user_model
from django.conf import settings

logger = logging.getLogger(__name__)


def _create_user_from_data(username, password, email, first_name="", last_name=""):
    """Shared user creation logic for registration and invitation acceptance.

    Returns (user, error_messages, status_code).
    If error_messages is not None, user creation failed.
    """
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    from job_hunting.models import Profile

    User = get_user_model()

    if not username:
        return None, [{"detail": "Username is required."}], 400
    if not password:
        return None, [{"detail": "Password is required."}], 400

    if User.objects.filter(username=username).exists():
        return None, [{"detail": "Username already exists."}], 400
    if email and User.objects.filter(email__iexact=email).exists():
        return None, [{"detail": "An account with this email already exists."}], 400

    try:
        validate_password(password)
    except ValidationError as e:
        return None, [{"detail": msg} for msg in e.messages], 400

    user = User(
        username=username, email=email,
        first_name=first_name, last_name=last_name,
    )
    user.set_password(password)
    user.save()

    Profile.objects.get_or_create(user_id=user.id)

    _notify_admins_new_signup(username, email, method="registration")

    return user, None, None


def _notify_admins_new_signup(username, email, method="registration"):
    """Email all superusers when someone signs up."""
    from django.core.mail import send_mail
    from django.template.loader import render_to_string
    from django.utils import timezone as tz

    User = get_user_model()
    admin_emails = list(
        User.objects.filter(is_superuser=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if not admin_emails:
        return

    try:
        body = render_to_string(
            "admin_new_signup.txt",
            {
                "username": username,
                "email": email or "(none)",
                "method": method,
                "timestamp": tz.now().strftime("%Y-%m-%d %H:%M UTC"),
            },
        )
        send_mail(
            subject=f"Career Caddy: new signup — {username}",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=admin_emails,
        )
    except Exception:
        logger.warning("Failed to send admin signup notification for %s", username)
