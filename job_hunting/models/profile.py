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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    linkedin = models.CharField(max_length=255, null=True, blank=True)
    github = models.CharField(max_length=255, null=True, blank=True)
    links = SafeJSONField(null=True, blank=True, default=dict)

    class Meta:
        db_table = "profile"

    def __str__(self):
        return f"Profile({self.user_id})"
