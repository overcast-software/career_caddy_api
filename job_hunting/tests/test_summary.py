from django.test import TestCase
from django.contrib.auth import get_user_model
from job_hunting.models import Summary


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

    def test_job_post_id_is_plain_integer(self):
        summary = Summary.objects.create(content="Test", job_post_id=42)
        self.assertEqual(summary.job_post_id, 42)
