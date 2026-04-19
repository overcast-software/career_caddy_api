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
URL = "/api/v1/reports/application-flow/"


def _status(name: str) -> Status:
    return Status.objects.get_or_create(status=name)[0]


def _log(app: JobApplication, status_name: str, *, days_ago: int = 0):
    when = timezone.now() - timedelta(days=days_ago)
    return JobApplicationStatus.objects.create(
        application=app,
        status=_status(status_name),
        logged_at=when,
    )


class TestApplicationFlowReport(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="flowuser", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def _attrs(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()["data"]["attributes"]

    def _edge(self, attrs, src_name: str, dst_name: str) -> int:
        ids = {n["id"]: i for i, n in enumerate(attrs["nodes"])}
        if src_name not in ids or dst_name not in ids:
            return 0
        total = 0
        for link in attrs["links"]:
            if link["source"] == ids[src_name] and link["target"] == ids[dst_name]:
                total += link["value"]
        return total

    def test_empty(self):
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_job_posts"], 0)
        self.assertEqual(attrs["total_applications"], 0)
        self.assertEqual(attrs["nodes"], [])
        self.assertEqual(attrs["links"], [])

    def test_job_post_without_application_routes_to_no_application(self):
        # Evaluable post (has a full description) but no application lands
        # in the no_application bucket — distinct from stub.
        JobPost.objects.create(
            title="No app",
            description=" ".join(["word"] * 30),
            company=self.company,
            created_by=self.user,
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_job_posts"], 1)
        self.assertEqual(attrs["total_applications"], 0)
        self.assertEqual(self._edge(attrs, "job_posts", "no_application"), 1)

    def test_thin_unscored_post_with_no_application_is_stub(self):
        # Email-pipeline-style post: title + link only, no description,
        # no score, no application. Should land in the 'stub' terminal.
        JobPost.objects.create(
            title="Stub", company=self.company, created_by=self.user
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_job_posts"], 1)
        self.assertEqual(self._edge(attrs, "job_posts", "stub"), 1)
        self.assertEqual(self._edge(attrs, "job_posts", "no_application"), 0)

    def test_scored_thin_post_is_not_stub(self):
        # User scored it — no longer "dead on arrival" even with thin desc.
        from job_hunting.models import Score
        jp = JobPost.objects.create(
            title="Scored but thin", company=self.company, created_by=self.user
        )
        Score.objects.create(job_post=jp, user=self.user, score=75)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "job_posts", "stub"), 0)
        self.assertEqual(self._edge(attrs, "job_posts", "no_application"), 1)

    def test_single_applied_application_no_ghost_yet(self):
        jp = JobPost.objects.create(title="P", company=self.company, created_by=self.user)
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Applied", days_ago=5)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "job_posts", "applications"), 1)
        self.assertEqual(self._edge(attrs, "applications", "applied"), 1)
        self.assertEqual(self._edge(attrs, "applied", "ghosted"), 0)

    def test_aged_applied_is_ghosted(self):
        jp = JobPost.objects.create(title="P", company=self.company, created_by=self.user)
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Applied", days_ago=45)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "applied", "ghosted"), 1)

    def test_full_happy_path_to_accepted(self):
        jp = JobPost.objects.create(title="P", company=self.company, created_by=self.user)
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Applied", days_ago=60)
        _log(app, "Interview Scheduled", days_ago=40)
        _log(app, "Offer", days_ago=10)
        _log(app, "Accepted", days_ago=5)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "applications", "applied"), 1)
        self.assertEqual(self._edge(attrs, "applied", "interview"), 1)
        self.assertEqual(self._edge(attrs, "interview", "offer"), 1)
        self.assertEqual(self._edge(attrs, "offer", "accepted"), 1)
        # terminal reached — no ghost edge
        self.assertEqual(self._edge(attrs, "accepted", "ghosted"), 0)

    def test_rejection_after_applied(self):
        jp = JobPost.objects.create(title="P", company=self.company, created_by=self.user)
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Applied", days_ago=10)
        _log(app, "Rejected", days_ago=3)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "applied", "rejected"), 1)

    def test_one_post_with_multiple_own_applications(self):
        jp = JobPost.objects.create(title="P", company=self.company, created_by=self.user)
        a1 = JobApplication.objects.create(job_post=jp, user=self.user)
        a2 = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(a1, "Applied", days_ago=5)
        _log(a2, "Applied", days_ago=5)
        _log(a2, "Rejected", days_ago=2)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 2)
        self.assertEqual(self._edge(attrs, "job_posts", "applications"), 2)

    def test_scope_all_requires_staff(self):
        response = self.client.get(URL + "?scope=all")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_scope_all_as_staff_aggregates_across_users(self):
        staff = User.objects.create_user(username="staff", password="pw", is_staff=True)
        other = User.objects.create_user(username="other", password="pw")
        jp = JobPost.objects.create(title="Shared", company=self.company, created_by=other)
        app = JobApplication.objects.create(job_post=jp, user=other)
        _log(app, "Applied", days_ago=5)

        self.client.force_authenticate(user=staff)
        attrs = self._attrs(self.client.get(URL + "?scope=all"))
        self.assertEqual(attrs["scope"], "all")
        self.assertGreaterEqual(attrs["total_job_posts"], 1)
        self.assertGreaterEqual(attrs["total_applications"], 1)
