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

    def test_unvetted_unscored_no_app_routes_through_both_hubs(self):
        # job_posts → unvetted → unscored → no_application.
        JobPost.objects.create(
            title="No app",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_job_posts"], 1)
        self.assertEqual(self._edge(attrs, "job_posts", "unvetted"), 1)
        self.assertEqual(self._edge(attrs, "unvetted", "unscored"), 1)
        self.assertEqual(self._edge(attrs, "unscored", "no_application"), 1)

    def test_thin_unscored_post_is_stub(self):
        # Thin description + untriaged + no application: job_posts →
        # unvetted → unscored → stub.
        JobPost.objects.create(
            title="Stub", company=self.company, created_by=self.user
        )
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "unvetted", "unscored"), 1)
        self.assertEqual(self._edge(attrs, "unscored", "stub"), 1)
        self.assertEqual(self._edge(attrs, "unscored", "no_application"), 0)

    def test_scored_post_routes_via_scored_hub(self):
        from job_hunting.models import Score
        jp = JobPost.objects.create(
            title="Scored",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        Score.objects.create(job_post=jp, user=self.user, score=75)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "unvetted", "scored"), 1)
        self.assertEqual(self._edge(attrs, "unvetted", "unscored"), 0)

    def test_vetted_good_without_score(self):
        # Vetted without a score: unvetted → unscored path NOT taken;
        # goes via vetted_good → unscored.
        jp = JobPost.objects.create(
            title="Vetted not scored",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Vetted Good", days_ago=3)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "job_posts", "vetted_good"), 1)
        self.assertEqual(self._edge(attrs, "vetted_good", "unscored"), 1)
        self.assertEqual(self._edge(attrs, "unscored", "no_application"), 1)

    def test_vetted_good_and_scored(self):
        from job_hunting.models import Score
        jp = JobPost.objects.create(
            title="Vetted + scored",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        Score.objects.create(job_post=jp, user=self.user, score=80)
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Vetted Good", days_ago=1)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "job_posts", "vetted_good"), 1)
        self.assertEqual(self._edge(attrs, "vetted_good", "scored"), 1)

    def test_vetted_bad_routes_via_vetted_bad_hub(self):
        jp = JobPost.objects.create(
            title="Rejected at vet",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Vetted Bad", days_ago=3)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "job_posts", "vetted_bad"), 1)
        self.assertEqual(self._edge(attrs, "vetted_bad", "unscored"), 1)
        # Vetted Bad also maps to BUCKET_REJECTED, so the app becomes
        # real and flows applications → rejected.
        self.assertEqual(self._edge(attrs, "unscored", "applications"), 1)
        self.assertEqual(self._edge(attrs, "applications", "rejected"), 1)

    def test_most_recent_triage_wins(self):
        jp = JobPost.objects.create(
            title="Changed mind",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Vetted Bad", days_ago=10)
        _log(app, "Vetted Good", days_ago=3)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "job_posts", "vetted_good"), 1)
        self.assertEqual(self._edge(attrs, "job_posts", "vetted_bad"), 0)

    def test_triage_plus_applied_counts_as_application(self):
        jp = JobPost.objects.create(
            title="Vetted then applied",
            description=" ".join(["word"] * 80),
            company=self.company,
            created_by=self.user,
        )
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Vetted Good", days_ago=10)
        _log(app, "Applied", days_ago=5)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(attrs["total_applications"], 1)
        self.assertEqual(self._edge(attrs, "unscored", "applications"), 1)
        self.assertEqual(self._edge(attrs, "applications", "applied"), 1)

    def test_single_applied_application_no_ghost_yet(self):
        jp = JobPost.objects.create(title="P", company=self.company, created_by=self.user)
        app = JobApplication.objects.create(job_post=jp, user=self.user)
        _log(app, "Applied", days_ago=5)
        attrs = self._attrs(self.client.get(URL))
        self.assertEqual(self._edge(attrs, "unscored", "applications"), 1)
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
        self.assertEqual(self._edge(attrs, "unscored", "applications"), 2)

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
