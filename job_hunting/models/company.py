from django.db import models
from .base import GetMixin


class Company(GetMixin, models.Model):
    name = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "company"

    def __str__(self):
        return self.display_name or self.name
