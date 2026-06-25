from django.db import models
from .base import GetMixin
from .nanoid_pk import NanoIDModel


class Answer(GetMixin, NanoIDModel):
    # ``id`` is the 10-char NanoID string PK from NanoIDModel (CC-77 #79
    # true PK swap). Answer is a leaf — nothing FKs to it — so the swap
    # only repoints its own PK.
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
