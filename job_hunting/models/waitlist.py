from .base import GetMixin
from django.db import models


class Waitlist(GetMixin, models.Model):
    email = models.EmailField(unique=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "waitlist"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Waitlist({self.email})"
