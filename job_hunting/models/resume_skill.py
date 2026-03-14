from django.db import models


class ResumeSkill(models.Model):
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.CASCADE,
        related_name="resume_skills",
        db_column="resume_id",
    )
    skill = models.ForeignKey(
        "Skill",
        on_delete=models.CASCADE,
        related_name="resume_skills",
        db_column="skill_id",
    )
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "resume_skill"
        unique_together = [("resume", "skill")]
