from django.db import models


class ResumeExperience(models.Model):
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.CASCADE,
        related_name="resume_experiences",
        db_column="resume_id",
    )
    experience = models.ForeignKey(
        "Experience",
        on_delete=models.CASCADE,
        related_name="resume_experiences",
        db_column="experience_id",
    )
    order = models.IntegerField(default=0)

    class Meta:
        db_table = "resume_experience"
