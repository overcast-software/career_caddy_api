from django.db import models


class ProjectDescription(models.Model):
    project = models.ForeignKey(
        "Project",
        on_delete=models.CASCADE,
        related_name="project_descriptions",
        db_column="project_id",
    )
    description = models.ForeignKey(
        "Description",
        on_delete=models.CASCADE,
        related_name="project_descriptions",
        db_column="description_id",
    )
    order = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_description"
