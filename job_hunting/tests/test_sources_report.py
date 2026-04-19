from datetime import timedelta

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Status,
)


User = get_user_model()
URL = "/api/v1/reports/sources/"


def _log(app, status_name, *, days_ago=0):
    when = timezone.now() - timedelta(days=days_ago)
    return JobApplicationStatus.objects.create(
        application=app,
        status=Status.objects.get_or_create(status=status_name)[0],
        logged_at=when,
    )


class TestSourcesReport(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="sr", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def _attrs(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()["data"]["attributes"]

    def _row(self, attrs, host):
        return next((r for r in attrs["rows"] if r["hostname"] == host), None)

    def _post(self, link):
        return JobPost.objects.create(
            title="T", company=self.company, created_by=self.user, link=link,
        )

    def test_empty(self):
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_job_posts"], 0)
        self.assertEqual(attrs["rows"], [])

    def test_post_without_application_lands_in_no_application(self):
        self._post("https://wellfound.com/jobs/1")
        attrs = self._attrs(self.client.get(URL))
        row = self._row(attrs, "wellfound.com")
        self.assertIsNotNone(row)
        self.assertEqual(row["total"], 1)
        self.assertEqual(row["buckets"].get("no_application"), 1)

    def test_aged_applied_is_ghosted(self):
        post = self._post("https://linkedin.com/jobs/1")
        app = JobApplication.objects.create(job_post=post, user=self.user)
        _log(app, "Applied", days_ago=45)
        attrs = self._attrs(self.client.get(URL))
        row = self._row(attrs, "linkedin.com")
        self.assertEqual(row["buckets"].get("ghosted"), 1)

    def test_same_hostname_multiple_outcomes_stack(self):
        for i in range(3):
            self._post(f"https://wellfound.com/jobs/open-{i}")
        hit = self._post("https://wellfound.com/jobs/interview")
        app = JobApplication.objects.create(job_post=hit, user=self.user)
        _log(app, "Applied", days_ago=7)
        _log(app, "Interview Scheduled", days_ago=1)
        attrs = self._attrs(self.client.get(URL))
        row = self._row(attrs, "wellfound.com")
        self.assertEqual(row["total"], 4)
        self.assertEqual(row["buckets"].get("no_application"), 3)
        self.assertEqual(row["buckets"].get("interview"), 1)

    def test_top_15_then_other(self):
        for i in range(20):
            self._post(f"https://site{i}.example/job/1")
        attrs = self._attrs(self.client.get(URL))
        hostnames = [r["hostname"] for r in attrs["rows"]]
        self.assertEqual(len(hostnames), 16)  # 15 + Other
        self.assertEqual(hostnames[-1], "Other")

    def test_no_link_bucketed_as_direct(self):
        self._post(None)
        attrs = self._attrs(self.client.get(URL))
        row = self._row(attrs, "(direct)")
        self.assertIsNotNone(row)
        self.assertEqual(row["total"], 1)

    def test_scope_all_requires_staff(self):
        response = self.client.get(URL + "?scope=all")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_scope_all_as_staff_sees_everyone(self):
        staff = User.objects.create_user(username="admin", password="pw", is_staff=True)
        other = User.objects.create_user(username="them", password="pw")
        JobPost.objects.create(
            title="Their", company=self.company, created_by=other,
            link="https://lever.co/jobs/1",
        )
        self.client.force_authenticate(user=staff)
        attrs = self._attrs(self.client.get(URL + "?scope=all"))
        self.assertEqual(attrs["scope"], "all")
        self.assertGreaterEqual(attrs["total_job_posts"], 1)
        self.assertTrue(any(r["hostname"] == "lever.co" for r in attrs["rows"]))
