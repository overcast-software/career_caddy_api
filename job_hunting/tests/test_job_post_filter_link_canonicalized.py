"""Regression test: filter[link]= must match canonical_link too.

The browser extension's popup-open lookup ("is this URL already in my
library?") sends the raw tab URL. The API's filter[link] used to do
exact-equality only, so a user landing on a URL with tracking params
(e.g. ?utm_source=...) wouldn't match the canonical-form post stored
under the rewritten URL.

After the fix, filter[link]=<raw> applies canonicalize_link to the input
and matches against either link or canonical_link.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost


User = get_user_model()


class TestJobPostFilterLinkCanonicalized(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="searcher", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        # JobPost.save auto-populates canonical_link via canonicalize_link
        # when only link is supplied.
        self.post = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            link="https://example.com/job/42",
            created_by=self.user,
        )

    def _ids(self, response):
        return {row["id"] for row in response.json()["data"]}

    def test_exact_link_still_matches(self):
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://example.com/job/42"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(self.post.id)})

    def test_link_with_tracking_params_matches_via_canonical(self):
        """User on the same job URL with utm tracking should still match."""
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://example.com/job/42?utm_source=newsletter"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(self.post.id)})

    def test_unrelated_link_does_not_match(self):
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://example.com/different/77"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), set())

    def test_link_lookup_finds_post_created_by_someone_else(self):
        """A user opening the popup on a URL someone ELSE has tracked must
        still see the existing JobPost. Without this the popup-open lookup
        regresses into a per-user filter and reports "not in your library"
        even when the dedupe pipeline would 409 on Send."""
        other_user = User.objects.create_user(username="other", password="p")
        other_post = JobPost.objects.create(
            title="Different Owner",
            company=self.company,
            link="https://example.com/job/99",
            created_by=other_user,
        )
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://example.com/job/99"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(other_post.id)})

    def test_default_list_still_per_user_scoped(self):
        """Regression: removing filter[link] must not leak other-user posts
        through the default list endpoint."""
        other_user = User.objects.create_user(username="other2", password="p")
        JobPost.objects.create(
            title="Hidden",
            company=self.company,
            link="https://example.com/hidden/1",
            created_by=other_user,
        )
        resp = self.client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        # self.user only authored self.post; the other_user's post must
        # not appear in the default list.
        self.assertEqual(self._ids(resp), {str(self.post.id)})
