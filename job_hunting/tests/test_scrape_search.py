from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape


User = get_user_model()
URL = "/api/v1/scrapes/"


class TestScrapeSearchFilter(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ss", password="pw")
        self.client.force_authenticate(user=self.user)
        self.wf = Company.objects.create(name="Wellfound", display_name="Wellfound")
        self.acme = Company.objects.create(name="Acme")
        self.wf_post = JobPost.objects.create(
            title="Backend Eng", company=self.wf, created_by=self.user
        )
        self.acme_post = JobPost.objects.create(
            title="Frontend Eng", company=self.acme, created_by=self.user
        )
        Scrape.objects.create(
            url="https://wellfound.com/jobs/123",
            job_post=self.wf_post,
            created_by=self.user,
            status="completed",
        )
        Scrape.objects.create(
            url="https://acme.test/jobs/456",
            job_post=self.acme_post,
            created_by=self.user,
            status="completed",
        )

    def _ids(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["id"] for row in response.json()["data"]}

    def test_no_filter_returns_all(self):
        ids = self._ids(self.client.get(URL))
        self.assertEqual(len(ids), 2)

    def test_query_matches_company_name(self):
        ids = self._ids(self.client.get(URL + "?filter[query]=wellfound"))
        self.assertEqual(len(ids), 1)

    def test_query_matches_url_substring(self):
        ids = self._ids(self.client.get(URL + "?filter[query]=acme.test"))
        self.assertEqual(len(ids), 1)

    def test_query_no_match(self):
        ids = self._ids(self.client.get(URL + "?filter[query]=zzznomatch"))
        self.assertEqual(len(ids), 0)
