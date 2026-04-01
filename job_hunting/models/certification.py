from django.db import models
from .base import GetMixin


class Certification(GetMixin, models.Model):
    issuer = models.CharField(max_length=255, null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    content = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "certification"

    def __str__(self):
        return self.title or self.issuer or ""

    def to_export_dict(self):
        return {
            "issuer": self.issuer or "",
            "title": self.title or "",
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "content": self.content or "",
        }
