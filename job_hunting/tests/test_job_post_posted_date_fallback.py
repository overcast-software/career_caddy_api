from datetime import date, timedelta

from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import JobPost


User = get_user_model()


class JobPostPostedDateFallbackTests(TestCase):
    """JobPost.save() backfills posted_date when the caller didn't set one.

    Driven by /job-posts sort=-posted_date NULLS LAST — without this,
    paste-sourced posts (which rarely come with a posted_date) get buried
    at the bottom of the pagination and look missing from page 1."""

    def setUp(self):
        self.user = User.objects.create_user(username="doug", password="p")

    def test_fallback_on_create_matches_created_at(self):
        jp = JobPost.objects.create(title="T", created_by=self.user)
        # save() runs before created_at is populated (auto_now_add writes
        # on INSERT), so the fallback uses timezone.now().date(). That
        # lands on the same UTC day as created_at.
        self.assertIsNotNone(jp.posted_date)
        self.assertEqual(jp.posted_date, jp.created_at.date())

    def test_explicit_posted_date_preserved(self):
        d = date(2025, 1, 15)
        jp = JobPost.objects.create(title="T", created_by=self.user, posted_date=d)
        self.assertEqual(jp.posted_date, d)

    def test_fallback_uses_created_at_date_on_later_save(self):
        jp = JobPost.objects.create(title="T", created_by=self.user, posted_date=date.today())
        self.assertIsNotNone(jp.created_at)
        # Null it out and re-save — fallback should land on created_at's date
        jp.posted_date = None
        jp.save()
        self.assertEqual(jp.posted_date, jp.created_at.date())

    def test_fallback_does_not_overwrite_existing(self):
        d = date.today() - timedelta(days=30)
        jp = JobPost.objects.create(title="T", created_by=self.user, posted_date=d)
        jp.title = "New Title"
        jp.save()
        jp.refresh_from_db()
        self.assertEqual(jp.posted_date, d)
