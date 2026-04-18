import json

from django.conf import settings
from django.db import models
from .base import GetMixin


class SafeJSONField(models.JSONField):
    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return json.loads(value)
        return value


class Profile(GetMixin, models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile_obj",
    )
    phone = models.CharField(max_length=50, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_guest = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    linkedin = models.CharField(max_length=255, null=True, blank=True)
    github = models.CharField(max_length=255, null=True, blank=True)
    links = SafeJSONField(null=True, blank=True, default=dict)
    onboarding = SafeJSONField(null=True, blank=True, default=dict)

    class Meta:
        db_table = "profile"

    def __str__(self):
        return f"Profile({self.user_id})"

    @staticmethod
    def default_onboarding():
        return {
            "wizard_enabled": True,
            "profile_basics": False,
            "resume_imported": False,
            "resume_reviewed": False,
            "first_job_post": False,
            "first_score": False,
            "first_cover_letter": False,
        }

    def resolved_onboarding(self):
        """Return stored onboarding merged on top of the default shape."""
        shape = self.default_onboarding()
        stored = self.onboarding if isinstance(self.onboarding, dict) else {}
        shape.update({k: v for k, v in stored.items() if k in shape})
        return shape

    def merge_onboarding(self, patch):
        """Merge a partial dict into onboarding (keys not in default shape are ignored)."""
        if not isinstance(patch, dict):
            return
        allowed = self.default_onboarding().keys()
        current = self.onboarding if isinstance(self.onboarding, dict) else {}
        current = {**current}
        for k, v in patch.items():
            if k in allowed:
                current[k] = bool(v)
        self.onboarding = current
