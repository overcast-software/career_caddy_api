"""Regression: PATCH /api/v1/scrapes/:id/ with `latest_status_note` in
the payload used to 500 with "property has no setter" because _upsert
blindly setattr'd every JSON:API attribute. The serializer now flags
the field as read_only_attributes — output stays, write is dropped.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Scrape


User = get_user_model()


class TestScrapePatchReadOnlyAttrs(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="patcher", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            created_by=self.user,
        )

    def _patch(self, attributes):
        return self.client.patch(
            f"/api/v1/scrapes/{self.scrape.id}/",
            data={
                "data": {
                    "type": "scrape",
                    "id": str(self.scrape.id),
                    "attributes": attributes,
                }
            },
            format="json",
        )

    def test_patch_with_latest_status_note_does_not_500(self):
        """Frontend round-trips the full record on save; latest_status_note
        rides along even though it's a derived property."""
        resp = self._patch({"status": "hold", "latest_status_note": "anything"})
        self.assertEqual(resp.status_code, 200)
        self.scrape.refresh_from_db()
        # Writable field stuck.
        self.assertEqual(self.scrape.status, "hold")

    def test_patch_with_only_latest_status_note_is_a_noop_not_a_500(self):
        resp = self._patch({"latest_status_note": "ignored"})
        self.assertEqual(resp.status_code, 200)
        # No real attribute changed; still alive.
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.status, "completed")

    def test_response_still_includes_latest_status_note_attribute(self):
        """Read-only must not mean invisible — frontend still reads it."""
        resp = self._patch({"status": "hold"})
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("latest_status_note", attrs)
