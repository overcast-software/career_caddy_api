from django.db import models


class JobPostDescriptionDecision(models.Model):
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.CASCADE,
        related_name="description_decisions",
    )
    triggering_scrape = models.ForeignKey(
        "Scrape",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="description_decisions",
    )
    existing_description_hash = models.CharField(max_length=64)
    new_description_hash = models.CharField(max_length=64)
    existing_word_count = models.IntegerField()
    new_word_count = models.IntegerField()
    existing_source = models.CharField(max_length=32, blank=True, default="")
    new_source = models.CharField(max_length=32, blank=True, default="")
    # keep_existing | use_new | merge
    choice = models.CharField(max_length=16)
    # high | medium | low
    confidence = models.CharField(max_length=8)
    reasoning = models.TextField(blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    duration_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "job_post_description_decision"
        indexes = [
            models.Index(fields=["job_post", "-created_at"]),
        ]
