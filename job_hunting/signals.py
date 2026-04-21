from django.db.models.signals import pre_delete
from django.dispatch import receiver

from job_hunting.models.resume import Resume
from job_hunting.models.score import Score


@receiver(pre_delete, sender=Resume)
def cascade_resume_delete_scores(sender, instance, **kwargs):
    """Resolve the career-data uniqueness collision before Score.resume
    SET_NULLs. A resume-specific Score (resume=<id>) becomes a career-data
    Score (resume=NULL) on cascade, which fires unique_score_per_job_user_career_data
    if a sibling career-data Score already exists for the same (job_post, user).
    Drop those superseded rows; let the rest get promoted to career-data.
    """
    resume_scores = Score.objects.filter(resume_id=instance.pk).values(
        "id", "job_post_id", "user_id"
    )
    superseded_ids = []
    for row in resume_scores:
        if row["job_post_id"] is None or row["user_id"] is None:
            continue
        sibling_exists = Score.objects.filter(
            job_post_id=row["job_post_id"],
            user_id=row["user_id"],
            resume__isnull=True,
        ).exists()
        if sibling_exists:
            superseded_ids.append(row["id"])
    if superseded_ids:
        Score.objects.filter(pk__in=superseded_ids).delete()
