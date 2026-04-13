from .base import GetMixin
from django.conf import settings
from django.db import models
from django.utils import timezone


class Invitation(GetMixin, models.Model):
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations_sent",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "invitation"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invitation({self.email})"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_accepted(self):
        return self.accepted_at is not None

    @property
    def is_valid(self):
        return not self.is_expired and not self.is_accepted
