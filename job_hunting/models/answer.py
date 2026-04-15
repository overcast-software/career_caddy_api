from django.db import models
from .base import GetMixin


class Answer(GetMixin, models.Model):
    question = models.ForeignKey(
        "Question",
        on_delete=models.CASCADE,
        related_name="answers",
    )
    content = models.TextField(null=True, blank=True)
    favorite = models.BooleanField(default=False)
    status = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "answer"
        ordering = ["-created_at"]
