from django.db import models
from .base import GetMixin


class Education(GetMixin, models.Model):
    degree = models.CharField(max_length=255, null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    institution = models.CharField(max_length=255, null=True, blank=True)
    major = models.CharField(max_length=255, null=True, blank=True)
    minor = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "education"

    def __str__(self):
        return self.institution or self.degree or ""

    def to_export_dict(self):
        return {
            "degree": self.degree or "",
            "institution": self.institution or "",
            "major": self.major or "",
            "minor": self.minor or "",
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
        }
