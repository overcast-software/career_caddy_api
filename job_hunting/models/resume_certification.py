from django.db import models


class ResumeCertification(models.Model):
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.CASCADE,
        related_name="resume_certifications",
        db_column="resume_id",
    )
    certification = models.ForeignKey(
        "Certification",
        on_delete=models.CASCADE,
        related_name="resume_certifications",
        db_column="certification_id",
    )
    issuer = models.CharField(max_length=255, null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    content = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "resume_certification"
