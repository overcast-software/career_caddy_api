"""Regression test: filter[query] on job-posts must match against the
`link` field too.

Inbox bug: user pasted https://talent.toptal.com/portal/job/X into the
/job-posts search and got zero results, even though a JobPost with that
exact link existed. They then re-parsed the URL just to discover the
duplicate — a wasted agent call. The fix added link__icontains to the
query filter; this test pins that behavior so it can't regress.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost


User = get_user_model()


class TestJobPostQueryFilterMatchesLink(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="searcher", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Toptal")
        self.match = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            link="https://talent.toptal.com/portal/job/VjEtSm9iLTQ5Mzg4OA",
            created_by=self.user,
        )
        self.other = JobPost.objects.create(
            title="Backend Engineer",
            company=self.company,
            link="https://example.com/jobs/42",
            created_by=self.user,
        )

    def _ids(self, response):
        return {row["id"] for row in response.json()["data"]}

    def test_full_url_query_finds_post_by_link(self):
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[query]": "https://talent.toptal.com/portal/job/VjEtSm9iLTQ5Mzg4OA"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(self.match.id)})

    def test_partial_url_query_still_matches_via_icontains(self):
        """A user pasting just the path slug should still find the post."""
        resp = self.client.get(
            "/api/v1/job-posts/", {"filter[query]": "VjEtSm9iLTQ5Mzg4OA"}
        )
        self.assertEqual(self._ids(resp), {str(self.match.id)})

    def test_query_still_matches_title_and_company(self):
        """Adding link to the OR-clause must not break existing matches."""
        title_resp = self.client.get(
            "/api/v1/job-posts/", {"filter[query]": "Senior"}
        )
        self.assertIn(str(self.match.id), self._ids(title_resp))

        company_resp = self.client.get(
            "/api/v1/job-posts/", {"filter[query]": "Toptal"}
        )
        self.assertEqual(
            self._ids(company_resp), {str(self.match.id), str(self.other.id)},
        )
