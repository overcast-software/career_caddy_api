from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape


User = get_user_model()
URL = "/api/v1/scrapes/"


class TestScrapeJobPostFilter(TestCase):
    """filter[job_post_id] scopes the scrape list to a single JobPost.

    Finding #3 / BACK-104 regression: cc_auto's forward-path enrich check
    queries GET /scrapes/?per_page=1&filter[job_post_id]=<new_post_id> to
    decide whether a freshly-created known-good post already has a claimable
    scrape. The list endpoint silently ignored the param and returned the
    principal's newest scrape regardless of post, so the check was a false
    positive whenever the principal had ANY scrape — stranding ZipRecruiter
    /km/ forwards with no scrape. These tests pin the filter so a post with
    no scrape correctly returns 0 and a post with one returns exactly it.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="jpf", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

        self.post_a = JobPost.objects.create(
            title="A", company=self.company, created_by=self.user
        )
        self.post_b = JobPost.objects.create(
            title="B", company=self.company, created_by=self.user
        )
        # A freshly-created post with NO scrape — the case the false positive
        # mis-reported as "already present".
        self.post_no_scrape = JobPost.objects.create(
            title="NoScrape", company=self.company, created_by=self.user
        )

        self.scrape_a = Scrape.objects.create(
            url="https://acme.test/a",
            job_post=self.post_a,
            created_by=self.user,
            status="hold",
        )
        self.scrape_b = Scrape.objects.create(
            url="https://acme.test/b",
            job_post=self.post_b,
            created_by=self.user,
            status="completed",
        )

    def _ids(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["id"] for row in response.json()["data"]}

    def test_filter_scopes_to_the_post(self):
        ids = self._ids(self.client.get(URL + f"?filter[job_post_id]={self.post_a.id}"))
        self.assertEqual(ids, {self.scrape_a.id})

    def test_relationship_alias_filter_job_post(self):
        ids = self._ids(self.client.get(URL + f"?filter[job_post]={self.post_b.id}"))
        self.assertEqual(ids, {self.scrape_b.id})

    def test_post_with_no_scrape_returns_empty(self):
        """The load-bearing case: a known-good post that has no scrape must
        return zero rows so cc_auto queues exactly one `hold` scrape."""
        resp = self.client.get(
            URL + f"?filter[job_post_id]={self.post_no_scrape.id}"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"], [])
        self.assertEqual(resp.json()["meta"]["total"], 0)

    def test_per_page_one_still_scopes(self):
        """per_page=1 (cc_auto's exact query shape) must still honor the
        filter — the false positive was per_page=1 returning the newest
        unrelated scrape."""
        resp = self.client.get(
            URL + f"?per_page=1&filter[job_post_id]={self.post_no_scrape.id}"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"], [])

    def test_no_filter_returns_all(self):
        ids = self._ids(self.client.get(URL))
        self.assertEqual(ids, {self.scrape_a.id, self.scrape_b.id})
