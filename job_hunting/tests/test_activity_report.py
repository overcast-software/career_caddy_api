from datetime import timedelta

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobApplication, JobPost


User = get_user_model()
URL = "/api/v1/reports/activity/"


class TestActivityReport(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="activityuser", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.job_post = JobPost.objects.create(
            title="Dev", company=self.company, created_by=self.user
        )

    def _attrs(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()["data"]["attributes"]

    def test_empty_365_day_window(self):
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 0)
        self.assertEqual(len(attrs["days"]), 366)
        self.assertTrue(all(d["count"] == 0 for d in attrs["days"]))

    def test_app_without_applied_at_does_not_count(self):
        JobApplication.objects.create(job_post=self.job_post, user=self.user)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 0)

    def test_applied_today_shows_on_today(self):
        now = timezone.now()
        JobApplication.objects.create(
            job_post=self.job_post, user=self.user, applied_at=now
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 1)
        today_iso = timezone.localdate().isoformat()
        today_entry = next(d for d in attrs["days"] if d["date"] == today_iso)
        self.assertEqual(today_entry["count"], 1)

    def test_multiple_apps_same_day_stack(self):
        now = timezone.now()
        for _ in range(3):
            JobApplication.objects.create(
                job_post=self.job_post, user=self.user, applied_at=now
            )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 3)
        today_iso = timezone.localdate().isoformat()
        today_entry = next(d for d in attrs["days"] if d["date"] == today_iso)
        self.assertEqual(today_entry["count"], 3)

    def test_date_range_filter(self):
        # Ten days ago
        then = timezone.now() - timedelta(days=10)
        JobApplication.objects.create(
            job_post=self.job_post, user=self.user, applied_at=then
        )
        # Narrow window that excludes it
        from_date = (timezone.localdate() - timedelta(days=3)).isoformat()
        to_date = timezone.localdate().isoformat()
        attrs = self._attrs(
            self.client.get(URL + f"?from={from_date}&to={to_date}")
        )
        self.assertEqual(attrs["total_applications"], 0)
        self.assertEqual(len(attrs["days"]), 4)  # today + 3 days back, inclusive

    def test_scope_all_requires_staff(self):
        response = self.client.get(URL + "?scope=all")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_scope_mine_excludes_other_users(self):
        other = User.objects.create_user(username="other", password="pw")
        JobApplication.objects.create(
            job_post=self.job_post, user=other, applied_at=timezone.now()
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 0)

    def test_status_series_bucketizes_log_events(self):
        # Log Applied + Interview on the same day — stacked area chart
        # should see 1 applied + 1 interview event that day.
        from job_hunting.models import JobApplicationStatus, Status
        app = JobApplication.objects.create(
            job_post=self.job_post, user=self.user, applied_at=timezone.now()
        )
        applied_status = Status.objects.get_or_create(status="Applied")[0]
        interview_status = Status.objects.get_or_create(
            status="Interview Scheduled"
        )[0]
        now = timezone.now()
        JobApplicationStatus.objects.create(
            application=app, status=applied_status, logged_at=now
        )
        JobApplicationStatus.objects.create(
            application=app, status=interview_status, logged_at=now
        )
        attrs = self._attrs(self.client.get(URL))
        series = attrs.get("status_series")
        self.assertIsNotNone(series)
        self.assertIn("applied", series["buckets"])
        self.assertIn("interview", series["buckets"])
        today_iso = timezone.localdate().isoformat()
        today = next(d for d in series["days"] if d["date"] == today_iso)
        self.assertEqual(today["applied"], 1)
        self.assertEqual(today["interview"], 1)
        self.assertEqual(series["total_events"], 2)

    def test_status_series_ignores_triage_labels(self):
        # Vetted Good is pre-application triage, not a real applied
        # event — the stacked chart should not double-count it.
        from job_hunting.models import JobApplicationStatus, Status
        app = JobApplication.objects.create(
            job_post=self.job_post, user=self.user
        )
        vetted = Status.objects.get_or_create(status="Vetted Good")[0]
        JobApplicationStatus.objects.create(
            application=app, status=vetted, logged_at=timezone.now()
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["status_series"]["total_events"], 0)
