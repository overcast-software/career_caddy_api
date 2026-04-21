from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Resume, Scrape, Score


User = get_user_model()
URL = "/api/v1/scrapes/"


class TestScrapeHasScoreFilter(TestCase):
    """filter[has_score] scopes scrapes by whether the linked JobPost
    carries at least one Score. Drives the auto-score daemon's candidate
    query; we filter here so the daemon doesn't N+1 through per-post
    lookups."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="hs", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.resume = Resume.objects.create(user=self.user, title="R")

        self.scored_post = JobPost.objects.create(
            title="Scored", company=self.company, created_by=self.user
        )
        self.unscored_post = JobPost.objects.create(
            title="Unscored", company=self.company, created_by=self.user
        )

        self.scored_scrape = Scrape.objects.create(
            url="https://acme.test/scored",
            job_post=self.scored_post,
            created_by=self.user,
            status="completed",
        )
        self.unscored_scrape = Scrape.objects.create(
            url="https://acme.test/unscored",
            job_post=self.unscored_post,
            created_by=self.user,
            status="completed",
        )
        self.orphan_scrape = Scrape.objects.create(
            url="https://acme.test/orphan",
            job_post=None,
            created_by=self.user,
            status="pending",
        )
        Score.objects.create(
            job_post=self.scored_post,
            resume=self.resume,
            user=self.user,
            score=80,
        )

    def _ids(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {int(row["id"]) for row in response.json()["data"]}

    def test_no_filter_returns_all(self):
        ids = self._ids(self.client.get(URL))
        self.assertEqual(
            ids,
            {self.scored_scrape.id, self.unscored_scrape.id, self.orphan_scrape.id},
        )

    def test_has_score_true_returns_only_scored(self):
        ids = self._ids(self.client.get(URL + "?filter[has_score]=true"))
        self.assertEqual(ids, {self.scored_scrape.id})

    def test_has_score_false_returns_only_unscored_with_job_post(self):
        """Scrapes without a linked job_post are dropped — the daemon can't
        score them anyway, and including them just wastes a row."""
        ids = self._ids(self.client.get(URL + "?filter[has_score]=false"))
        self.assertEqual(ids, {self.unscored_scrape.id})
        self.assertNotIn(self.orphan_scrape.id, ids)
