from django.db import models
from django.db.models import Q
from .base import GetMixin


class Company(GetMixin, models.Model):
    name = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "company"

    def __str__(self):
        return self.display_name or self.name


def find_matching_companies(name: str):
    """All Companies whose name OR display_name contains `name`
    (case-insensitive). Returns every plausible row, not just one — "Disney"
    must also surface "Disney, Inc". Shared by CompanyViewSet.list() and the
    raw-fields duplicate-candidates endpoint so both fuzzy-match identically.
    """
    if not name:
        return Company.objects.none()
    return Company.objects.filter(
        Q(name__icontains=name) | Q(display_name__icontains=name)
    ).distinct()
