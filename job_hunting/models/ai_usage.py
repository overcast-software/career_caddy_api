from decimal import Decimal

from django.conf import settings
from django.db import models

from .base import GetMixin


class AiUsage(GetMixin, models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_usages",
    )

    # What ran (the "how" dimension)
    agent_name = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100)
    trigger = models.CharField(max_length=50)
    pipeline_run_id = models.UUIDField(null=True, blank=True, db_index=True)

    # Token counts (from pydantic-ai Usage object)
    request_tokens = models.IntegerField(default=0)
    response_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    request_count = models.IntegerField(default=1)

    # Cost (computed server-side from pricing lookup)
    estimated_cost_usd = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        default=Decimal("0"),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_usage"
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["agent_name", "created_at"]),
        ]
