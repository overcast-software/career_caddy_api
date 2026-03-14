from django.db import models


class ResumeProject(models.Model):
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.CASCADE,
        related_name="resume_projects",
        db_column="resume_id",
    )
    project = models.ForeignKey(
        "Project",
        on_delete=models.CASCADE,
        related_name="resume_projects",
        db_column="project_id",
    )
    order = models.IntegerField(default=0)

    class Meta:
        db_table = "resume_project"
