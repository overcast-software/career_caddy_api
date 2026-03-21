from django.conf import settings
from django.db import models


class Project(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="projects",
    )
    title = models.CharField(max_length=255, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project"

    @property
    def descriptions(self):
        from job_hunting.models.project_description import ProjectDescription
        from job_hunting.models.description import Description

        desc_ids = list(
            ProjectDescription.objects.filter(project_id=self.id)
            .order_by("order")
            .values_list("description_id", flat=True)
        )
        return list(Description.objects.filter(pk__in=desc_ids))
