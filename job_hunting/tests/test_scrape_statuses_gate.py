"""GET /api/v1/scrapes/:id/scrape-statuses/ should be owner-or-staff
gated. The endpoint emits the full graph_payload, which can carry
exception text and internal-only diagnostic detail; we don't want
non-owner non-staff users reading another user's scrape diagnostics.

Mirrors the gate already on /scrapes/:id/graph-trace/.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Scrape


User = get_user_model()


class ScrapeStatusesGateTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="p")
        self.other = User.objects.create_user(username="other", password="p")
        self.staff = User.objects.create_user(
            username="staff", password="p", is_staff=True
        )
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="failed",
            created_by=self.owner,
            source="scrape",
        )

    def _get(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"/api/v1/scrapes/{self.scrape.id}/scrape-statuses/")

    def test_owner_can_read(self):
        self.assertEqual(self._get(self.owner).status_code, 200)

    def test_staff_can_read(self):
        self.assertEqual(self._get(self.staff).status_code, 200)

    def test_other_user_forbidden(self):
        self.assertEqual(self._get(self.other).status_code, 403)

    def test_unknown_scrape_404(self):
        client = APIClient()
        client.force_authenticate(user=self.owner)
        resp = client.get("/api/v1/scrapes/999999/scrape-statuses/")
        self.assertEqual(resp.status_code, 404)
