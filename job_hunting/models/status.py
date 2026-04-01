from django.db import models
from .base import GetMixin


class Status(GetMixin, models.Model):
    status = models.CharField(max_length=255)
    status_type = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "status"

    def __str__(self):
        return self.status
