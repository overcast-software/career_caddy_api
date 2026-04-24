"""Regression: GET /api/v1/job-applications/:id/?include=application-statuses
must return hasMany linkage data so the frontend's <Applications::StatusLog>
can render the history. Without the linkage, Ember Data treats the hasMany
as empty and the UI shows "No history yet" even when the DB has rows —
exactly the bug on /job-applications/92.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Status,
)


User = get_user_model()


class TestJobApplicationStatusHistoryPayload(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hist", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.job_post = JobPost.objects.create(title="Dev", company=self.company)
        self.application = JobApplication.objects.create(
            user=self.user, job_post=self.job_post, company=self.company
        )
        # Seed 3 statuses so the hasMany has something to render.
        self.statuses = []
        for name in ("Applied", "Vetted Good", "Contact"):
            status_rec, _ = Status.objects.get_or_create(
                status=name, status_type="application"
            )
            self.statuses.append(
                JobApplicationStatus.objects.create(
                    application=self.application, status=status_rec
                )
            )

    def _retrieve(self, **query):
        return self.client.get(f"/api/v1/job-applications/{self.application.id}/", query)

    def test_response_includes_hasmany_linkage_data(self):
        """Without `data: [...]` on the relationship, Ember Data can't
        populate applicationStatuses from `included`."""
        resp = self._retrieve()
        self.assertEqual(resp.status_code, 200)
        rels = resp.json()["data"]["relationships"]
        self.assertIn("application-statuses", rels)
        linkage = rels["application-statuses"].get("data")
        self.assertIsInstance(
            linkage, list, "application-statuses must emit `data` linkage array"
        )
        self.assertEqual(
            {row["id"] for row in linkage},
            {str(s.id) for s in self.statuses},
        )
        for row in linkage:
            self.assertEqual(row["type"], "job-application-status")

    def test_include_query_sideloads_the_status_records(self):
        """The frontend passes ?include=application-statuses; response's
        included array must carry the full records so Ember can match
        them against the linkage."""
        resp = self._retrieve(**{"include": "application-statuses"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        included = body.get("included", [])
        ids = {row["id"] for row in included if row["type"] == "job-application-status"}
        self.assertEqual(ids, {str(s.id) for s in self.statuses})
