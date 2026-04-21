from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost, Resume, Score

User = get_user_model()


class TestResumeDeleteCascadesScores(TestCase):
    """Regression for prod 500: DELETE /api/v1/resumes/:id/ blew up with
    IntegrityError on unique_score_per_job_user_career_data when the resume
    had a Score sharing (job_post, user) with an existing career-data Score.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.company = Company.objects.create(name="Acme")
        self.job = JobPost.objects.create(title="Eng", company=self.company)
        self.resume = Resume.objects.create(user=self.user, title="R1")

    def test_superseded_resume_score_deleted_not_setnulled(self):
        Score.objects.create(
            job_post=self.job, user=self.user, resume=None, score=70
        )
        resume_score = Score.objects.create(
            job_post=self.job, user=self.user, resume=self.resume, score=85
        )
        self.resume.delete()
        self.assertFalse(Score.objects.filter(pk=resume_score.pk).exists())
        self.assertEqual(
            Score.objects.filter(
                job_post=self.job, user=self.user, resume__isnull=True
            ).count(),
            1,
        )

    def test_no_sibling_promotes_to_career_data(self):
        resume_score = Score.objects.create(
            job_post=self.job, user=self.user, resume=self.resume, score=85
        )
        self.resume.delete()
        resume_score.refresh_from_db()
        self.assertIsNone(resume_score.resume_id)
        self.assertEqual(resume_score.score, 85)

    def test_other_users_career_data_score_untouched(self):
        other = User.objects.create_user(username="other", password="p")
        other_score = Score.objects.create(
            job_post=self.job, user=other, resume=None, score=60
        )
        Score.objects.create(
            job_post=self.job, user=self.user, resume=self.resume, score=85
        )
        self.resume.delete()
        other_score.refresh_from_db()
        self.assertEqual(other_score.score, 60)
