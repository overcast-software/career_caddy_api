from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Resume, Scrape


User = get_user_model()


class TestSparseFieldsetsScrape(TestCase):
    """JSON:API `fields[<type>]` opt-in attribute filter on the scrape list.

    The list endpoint used to return every Scrape attribute on every row
    — including `job_content`, `html`, and `apply_candidates`, which can be
    tens to hundreds of KB each. With ~250 rows that ballooned the
    response past 5MB. Sparse fieldsets let the frontend ask for only the
    columns it actually renders.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="sf", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="Eng", company=self.company, created_by=self.user
        )
        Scrape.objects.create(
            url="https://acme.test/jobs/1",
            job_post=self.post,
            created_by=self.user,
            status="completed",
            job_content="A" * 10000,  # would dominate response without filter
            html="<html>" + "B" * 10000 + "</html>",
        )

    def test_no_filter_returns_all_attributes(self):
        resp = self.client.get("/api/v1/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertIn("url", attrs)
        self.assertIn("status", attrs)
        self.assertIn("job_content", attrs)
        self.assertIn("html", attrs)

    def test_fields_filter_drops_unrequested_attributes(self):
        resp = self.client.get("/api/v1/scrapes/?fields[scrape]=url,status")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertEqual(set(attrs.keys()), {"url", "status"})
        # The heavy fields must NOT be present — that's the whole point.
        self.assertNotIn("job_content", attrs)
        self.assertNotIn("html", attrs)

    def test_fields_filter_ignores_unknown_attributes(self):
        # Garbage in fields[scrape] should be silently dropped, not 500.
        resp = self.client.get(
            "/api/v1/scrapes/?fields[scrape]=url,not_a_real_attr"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertEqual(set(attrs.keys()), {"url"})

    def test_fields_filter_applies_to_retrieve(self):
        scrape_id = Scrape.objects.first().id
        resp = self.client.get(
            f"/api/v1/scrapes/{scrape_id}/?fields[scrape]=status"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(set(attrs.keys()), {"status"})


class TestSparseFieldsetsResume(TestCase):
    """Resume serializer overrides to_resource() to inject a `summary`
    attribute computed from active_summary_content(). It must respect
    fields[resume] just like declared attributes."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="rf", password="pw")
        self.client.force_authenticate(user=self.user)
        self.resume = Resume.objects.create(
            user=self.user, title="Main", name="main"
        )

    def test_summary_omitted_when_not_in_fields(self):
        resp = self.client.get(
            f"/api/v1/resumes/{self.resume.id}/?fields[resume]=name,title"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(set(attrs.keys()), {"name", "title"})
        self.assertNotIn("summary", attrs)

    def test_summary_emitted_when_in_fields(self):
        resp = self.client.get(
            f"/api/v1/resumes/{self.resume.id}/?fields[resume]=name,summary"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("summary", attrs)
        self.assertIn("name", attrs)
        self.assertNotIn("title", attrs)

    def test_summary_default_present_without_filter(self):
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("summary", attrs)
        self.assertIn("title", attrs)
