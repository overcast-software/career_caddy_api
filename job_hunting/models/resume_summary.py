from django.db import models


class ResumeSummary(models.Model):
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.CASCADE,
        related_name="resume_summaries",
        db_column="resume_id",
    )
    summary = models.ForeignKey(
        "Summary",
        on_delete=models.CASCADE,
        related_name="resume_summaries",
        db_column="summary_id",
    )
    active = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "resume_summaries"

    @classmethod
    def ensure_single_active_for_resume(cls, resume_id):
        links = list(cls.objects.filter(resume_id=resume_id))
        if not links:
            return
        actives = [lnk for lnk in links if lnk.active]
        if len(actives) == 1:
            return
        if len(actives) == 0:
            keep_id = max(lnk.id for lnk in links)
        else:
            keep_id = max(lnk.id for lnk in actives)
        cls.objects.filter(resume_id=resume_id).update(active=False)
        cls.objects.filter(pk=keep_id).update(active=True)
