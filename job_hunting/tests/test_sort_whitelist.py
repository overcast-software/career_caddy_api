"""CC-93: list endpoints whitelist `?sort=` fields and return 400 (NOT 500)
on an unknown/unsupported field.

Regression: `GET /api/v1/job-posts/?sort=-updated_at` used to 500 because
`JobPost` has no `updated_at` column — the unguarded `order_by(F(name)...)`
raised a Django FieldError at `qs.count()`. The same unguarded pattern lived
in the scrapes, companies and job-applications list views. Every list path
must now reject an unknown sort field with a clean 400 while still honoring a
valid sort.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobApplication, JobPost, Scrape


User = get_user_model()


class SortWhitelistTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="sorter", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            link="https://acme.example/jobs/1",
            description="x" * 500,
            created_by=self.user,
        )
        self.application = JobApplication.objects.create(
            user=self.user,
            job_post=self.post,
            company=self.company,
            status="applied",
        )
        self.scrape = Scrape.objects.create(
            url="https://acme.example/jobs/1",
            job_post=self.post,
            created_by=self.user,
            status="completed",
        )

    def _assert_sort_error(self, resp):
        """An unknown sort field must yield a clean JSON:API 400, never a 500."""
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        body = resp.json()
        self.assertIn("errors", body)
        err = body["errors"][0]
        self.assertEqual(err.get("source", {}).get("parameter"), "sort")
        # The offending field name should surface in the detail message.
        self.assertIn("updated_at", err.get("detail", ""))

    # --- unknown field -> 400 (the CC-93 regression) -----------------------

    def test_job_posts_unknown_sort_returns_400_not_500(self):
        resp = self.client.get("/api/v1/job-posts/?sort=-updated_at")
        self._assert_sort_error(resp)

    def test_job_applications_unknown_sort_returns_400_not_500(self):
        resp = self.client.get("/api/v1/job-applications/?sort=-updated_at")
        self._assert_sort_error(resp)

    def test_scrapes_unknown_sort_returns_400_not_500(self):
        resp = self.client.get("/api/v1/scrapes/?sort=-updated_at")
        self._assert_sort_error(resp)

    def test_companies_unknown_sort_returns_400_not_500(self):
        resp = self.client.get("/api/v1/companies/?sort=-updated_at")
        self._assert_sort_error(resp)

    def test_unknown_field_among_valid_ones_still_rejected(self):
        # A whitelisted field plus an unknown one must still 400 — don't
        # silently drop the bad one.
        resp = self.client.get("/api/v1/job-posts/?sort=title,-updated_at")
        self._assert_sort_error(resp)

    # --- valid sort still works -> 200 -------------------------------------

    def test_job_posts_valid_sort_ok(self):
        resp = self.client.get("/api/v1/job-posts/?sort=-posted_date")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r["id"] for r in resp.json()["data"]}
        self.assertIn(str(self.post.id), ids)

    def test_job_applications_valid_sort_ok(self):
        resp = self.client.get("/api/v1/job-applications/?sort=-applied_at")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_scrapes_valid_sort_ok(self):
        resp = self.client.get("/api/v1/scrapes/?sort=-scraped_at")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r["id"] for r in resp.json()["data"]}
        self.assertIn(str(self.scrape.id), ids)

    def test_companies_valid_sort_ok(self):
        resp = self.client.get("/api/v1/companies/?sort=name")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r["id"] for r in resp.json()["data"]}
        self.assertIn(str(self.company.id), ids)

    def test_companies_default_relevant_sort_unaffected(self):
        # The `relevant` annotation path is not a real column; it must keep
        # working (and not be whitelist-checked).
        resp = self.client.get("/api/v1/companies/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
