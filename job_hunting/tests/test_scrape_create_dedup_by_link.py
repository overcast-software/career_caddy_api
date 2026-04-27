"""POST /api/v1/scrapes/ with status=hold should refuse to mint a
scrape when the URL already maps to a JobPost. Closes the chat-agent
half of the dedup-by-link story at the tool layer (the prompt rule
shipped 2026-04-26 in ai PR #14; this is the belt to that
suspenders).

Returns 409 with errors[0].meta.existing_job_post_id (no `data` key)
so Ember Data clients calling createRecord('scrape').save() don't
get a JobPost pushed into the in-flight scrape identifier — a 200 +
data.type='job-post' previously corrupted the store with a lid
collision (scrape:null trying to take id N already held by job-post:N).
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, Scrape


User = get_user_model()


class ScrapeCreateDedupByLinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def _create_hold(self, url, **extra):
        body = {
            "data": {
                "attributes": {"url": url, "status": "hold", **extra},
            }
        }
        return self.client.post("/api/v1/scrapes/", body, format="json")

    def test_returns_409_with_existing_job_post_id_when_url_matches(self):
        jp = JobPost.objects.create(
            title="Senior Widget Engineer",
            company=self.company,
            link="https://acme.example/jobs/42",
            created_by=self.user,
        )
        resp = self._create_hold("https://acme.example/jobs/42")
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertNotIn("data", body)
        err = body["errors"][0]
        self.assertEqual(err["code"], "duplicate")
        self.assertEqual(err["meta"]["existing_job_post_id"], jp.id)
        # No scrape row should have been minted.
        self.assertFalse(
            Scrape.objects.filter(url="https://acme.example/jobs/42").exists()
        )

    def test_strips_tracking_params_when_matching(self):
        """A URL with utm_* tracking params should still match the canonical
        post (canonical_link is populated on save())."""
        jp = JobPost.objects.create(
            title="Trk Test",
            company=self.company,
            link="https://acme.example/jobs/77",
            created_by=self.user,
        )
        resp = self._create_hold(
            "https://acme.example/jobs/77?utm_source=newsletter&utm_medium=email"
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()["errors"][0]["meta"]["existing_job_post_id"], jp.id
        )
        self.assertFalse(
            Scrape.objects.filter(url__contains="utm_source").exists()
        )

    def test_creates_scrape_when_no_match(self):
        resp = self._create_hold("https://other.example/jobs/1")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["data"]["type"], "scrape")
        self.assertTrue(
            Scrape.objects.filter(url="https://other.example/jobs/1").exists()
        )

    def test_match_by_raw_link_when_canonical_unset(self):
        """Defensive fallback for any historical row without canonical_link
        populated. save() backfills canonical_link, but force-null the
        column on an existing row to exercise the fallback path."""
        jp = JobPost.objects.create(
            title="Legacy",
            company=self.company,
            link="https://legacy.example/jobs/9",
            created_by=self.user,
        )
        # Force canonical_link to None to exercise the raw-link fallback.
        JobPost.objects.filter(pk=jp.id).update(canonical_link=None)
        resp = self._create_hold("https://legacy.example/jobs/9")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()["errors"][0]["meta"]["existing_job_post_id"], jp.id
        )
