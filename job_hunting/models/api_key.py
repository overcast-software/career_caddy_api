import secrets
import hashlib
import json
from datetime import timedelta

from django.conf import settings
from .base import GetMixin
from django.db import models
from django.utils import timezone


class ApiKey(GetMixin, models.Model):
    name = models.CharField(max_length=255, null=True, blank=True)
    key_hash = models.CharField(max_length=255, null=True, blank=True, unique=True)
    key_prefix = models.CharField(max_length=20, null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="api_keys",
    )
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(null=True, blank=True)  # JSON array string
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "api_keys"

    @classmethod
    def generate_key(cls, name: str, user_id: int, expires_days: int = None, scopes: list = None):
        """Generate a new API key. Returns (instance, raw_key)."""
        raw_key = f"jh_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:12]

        expires_at = None
        if expires_days:
            expires_at = timezone.now() + timedelta(days=expires_days)

        scopes_json = json.dumps(scopes) if scopes else None

        obj = cls.objects.create(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            user_id=user_id,
            expires_at=expires_at,
            scopes=scopes_json,
        )
        return obj, raw_key

    @classmethod
    def authenticate(cls, key: str):
        """Authenticate a raw API key. Returns instance or None."""
        if not key or not key.startswith("jh_"):
            return None

        key_hash = hashlib.sha256(key.encode()).hexdigest()
        obj = cls.objects.filter(key_hash=key_hash, is_active=True).first()
        if not obj:
            return None

        if obj.expires_at and obj.expires_at < timezone.now():
            return None

        obj.last_used_at = timezone.now()
        obj.save(update_fields=["last_used_at"])
        return obj

    def get_scopes(self):
        """Return scopes as a list."""
        if not self.scopes:
            return []
        try:
            return json.loads(self.scopes)
        except (json.JSONDecodeError, TypeError):
            return []

    def has_scope(self, scope: str):
        """Check if key has a specific scope."""
        scopes = self.get_scopes()
        return scope in scopes or "*" in scopes

    def revoke(self):
        """Revoke the API key."""
        self.is_active = False
        self.save(update_fields=["is_active"])
