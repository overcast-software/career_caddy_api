"""Tests for POST /api/v1/job-posts/{id}/resolve-and-dedupe/.

Staff-only endpoint that creates a Scrape with skip_extract=True linked
to the JobPost. The hold-poller picks it up and the scrape-graph runs
Navigate → ResolveFinalUrl → CheckLinkDedup → (page-load) →
ResolveApplyUrl → End, skipping the LLM extract chain. Used by the
"Resolve & dedupe" pill on jp.edit to settle JS / meta-refresh
redirects (e.g. ZipRecruiter /km/<token>) and surface dedupe candidates
without overwriting the originating JP's fields.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, Scrape


User = get_user_model()


class ResolveAndDedupeEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.staff = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )
        self.client = APIClient()

    def test_non_staff_owner_denied(self):
        """Even the JP's creator can't trigger this — staff-only."""
        self.client.force_authenticate(user=self.user)
        jp = JobPost.objects.create(
            title="T",
            link="https://example.com/jobs/1",
            created_by=self.user,
        )
        resp = self.client.post(
            f"/api/v1/job-posts/{jp.id}/resolve-and-dedupe/"
        )
        self.assertEqual(resp.status_code, 403)

    def test_staff_creates_skip_extract_hold_scrape(self):
        """Staff POST creates Scrape(skip_extract=True, status=hold,
        url=jp.link, source=manual) linked to the JP."""
        self.client.force_authenticate(user=self.staff)
        jp = JobPost.objects.create(
            title="Senior Engineer",
            link="https://www.ziprecruiter.com/km/AAHQDn_opaque",
            created_by=self.user,  # not the staff user — endpoint allows it
        )
        resp = self.client.post(
            f"/api/v1/job-posts/{jp.id}/resolve-and-dedupe/"
        )
        self.assertEqual(resp.status_code, 202)

        scrapes = Scrape.objects.filter(job_post=jp)
        self.assertEqual(scrapes.count(), 1)
        scrape = scrapes.first()
        self.assertTrue(scrape.skip_extract)
        self.assertEqual(scrape.status, "hold")
        self.assertEqual(scrape.url, jp.link)
        self.assertEqual(scrape.source, "manual")
        self.assertEqual(scrape.created_by_id, self.staff.id)
        self.assertEqual(scrape.job_post_id, jp.id)

    def test_response_carries_scrape_resource(self):
        """Response body has the new Scrape so the client can poll."""
        self.client.force_authenticate(user=self.staff)
        jp = JobPost.objects.create(
            title="T",
            link="https://example.com/track/abc",
            created_by=self.user,
        )
        resp = self.client.post(
            f"/api/v1/job-posts/{jp.id}/resolve-and-dedupe/"
        )
        body = resp.json()
        self.assertIn("data", body)
        self.assertEqual(body["data"]["type"], "scrape")
        self.assertTrue(body["data"]["attributes"]["skip_extract"])
        self.assertEqual(body["data"]["attributes"]["status"], "hold")

    def test_company_relationship_copied_when_present(self):
        """When the JP has a company, the Scrape inherits it so the
        graph's company-aware paths work."""
        self.client.force_authenticate(user=self.staff)
        company = Company.objects.create(name="Acme")
        jp = JobPost.objects.create(
            title="T",
            link="https://example.com/jobs/1",
            company=company,
            created_by=self.user,
        )
        self.client.post(
            f"/api/v1/job-posts/{jp.id}/resolve-and-dedupe/"
        )
        scrape = Scrape.objects.filter(job_post=jp).first()
        self.assertEqual(scrape.company_id, company.id)

    def test_jp_without_link_returns_400(self):
        """Nothing to resolve — surface a clear error rather than
        creating a no-op scrape."""
        self.client.force_authenticate(user=self.staff)
        jp = JobPost.objects.create(
            title="T", link="", created_by=self.user
        )
        resp = self.client.post(
            f"/api/v1/job-posts/{jp.id}/resolve-and-dedupe/"
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["errors"][0]["code"], "no_link")
        self.assertEqual(Scrape.objects.filter(job_post=jp).count(), 0)

    def test_unknown_jp_returns_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            "/api/v1/job-posts/999999/resolve-and-dedupe/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_jp_does_not_get_field_overwrites(self):
        """The whole point of this action: leave the JP untouched.
        Endpoint creates a Scrape, doesn't modify JP fields."""
        self.client.force_authenticate(user=self.staff)
        jp = JobPost.objects.create(
            title="Original Title",
            link="https://example.com/jobs/1",
            description="Original description",
            created_by=self.user,
            complete=True,
        )
        self.client.post(
            f"/api/v1/job-posts/{jp.id}/resolve-and-dedupe/"
        )
        jp.refresh_from_db()
        self.assertEqual(jp.title, "Original Title")
        self.assertEqual(jp.description, "Original description")
        self.assertTrue(jp.complete)
