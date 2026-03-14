from django.db import models


class ResumeEducation(models.Model):
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.CASCADE,
        related_name="resume_educations",
        db_column="resume_id",
    )
    education = models.ForeignKey(
        "Education",
        on_delete=models.CASCADE,
        related_name="resume_educations",
        db_column="education_id",
    )
    institution = models.CharField(max_length=255, null=True, blank=True)
    degree = models.CharField(max_length=255, null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    content = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "resume_education"
