from django.db import models
from .base import GetMixin
from .nanoid_pk import NanoIDModel


class Skill(GetMixin, NanoIDModel):
    text = models.CharField(max_length=255, null=True, blank=True)
    skill_type = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "skill"

    def __str__(self):
        return self.text or ""

    def to_export_value(self):
        return {"text": self.text, "skill_type": self.skill_type}
