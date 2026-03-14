from django.db import models


class ExperienceDescription(models.Model):
    experience = models.ForeignKey(
        "Experience",
        on_delete=models.CASCADE,
        related_name="experience_descriptions",
        db_column="experience_id",
    )
    description = models.ForeignKey(
        "Description",
        on_delete=models.CASCADE,
        related_name="experience_descriptions",
        db_column="description_id",
    )
    order = models.IntegerField(default=0)

    class Meta:
        db_table = "experience_description"
        unique_together = [("experience", "description")]
