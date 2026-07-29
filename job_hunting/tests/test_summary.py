from django.test import TestCase
from django.contrib.auth import get_user_model
from job_hunting.models import Company, JobPost, Summary


class SummaryModelTests(TestCase):
    def test_create_summary(self):
        summary = Summary.objects.create(content="Experienced software engineer.")
        self.assertEqual(summary.content, "Experienced software engineer.")
        self.assertIsNone(summary.job_post_id)
        self.assertIsNone(summary.user)

    def test_content_nullable(self):
        summary = Summary.objects.create()
        self.assertIsNone(summary.content)

    def test_user_relationship(self):
        User = get_user_model()
        user = User.objects.create_user(username="summaryuser", password="pass")
        summary = Summary.objects.create(content="Test", user=user)
        self.assertEqual(summary.user, user)

    def test_job_post_is_nanoid_foreign_key(self):
        # CC-218: job_post_id is now a real FK onto JobPost's 10-char NanoID
        # string PK (was a bare IntegerField that couldn't reference real
        # posts). The column stores the NanoID string and traverses the FK.
        company = Company.objects.create(name="Acme")
        jp = JobPost.objects.create(
            title="Engineer", company=company, description="x " * 40
        )
        self.assertIsInstance(jp.id, str)
        summary = Summary.objects.create(content="Test", job_post=jp)
        summary.refresh_from_db()
        self.assertEqual(summary.job_post_id, jp.id)
        self.assertIsInstance(summary.job_post_id, str)
        self.assertEqual(summary.job_post.title, "Engineer")
