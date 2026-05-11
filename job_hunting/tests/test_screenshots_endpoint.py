"""GET /api/v1/scrapes/:id/screenshots/ returns JSON:API resources."""

import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.models.scrape import Scrape


class ScreenshotsEndpointTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.staff = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            created_by=self.staff,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_screenshot(self, filename: str, content: bytes = b"\x89PNG\r\n\x1a\n"):
        scrape_dir = Path(self.tmp.name) / str(self.scrape.id)
        scrape_dir.mkdir(parents=True, exist_ok=True)
        path = scrape_dir / filename
        path.write_bytes(content)
        return path

    def test_returns_jsonapi_resource_list(self):
        self._seed_screenshot("first.png")
        self._seed_screenshot("second.png")
        with override_settings(SCREENSHOT_DIR=self.tmp.name):
            resp = self.client.get(
                f"/api/v1/scrapes/{self.scrape.id}/screenshots/"
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["data"]), 2)
        for row in body["data"]:
            self.assertEqual(row["type"], "screenshot")
            self.assertIn("filename", row["attributes"])
            self.assertIn("size", row["attributes"])
            self.assertIn("taken_at", row["attributes"])
            self.assertEqual(
                row["relationships"]["scrape"]["data"]["id"],
                str(self.scrape.id),
            )
        # Composite id keeps store identity stable across multiple scrapes.
        ids = {row["id"] for row in body["data"]}
        self.assertEqual(
            ids,
            {f"{self.scrape.id}/first.png", f"{self.scrape.id}/second.png"},
        )
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_empty_list_when_no_screenshots(self):
        with override_settings(SCREENSHOT_DIR=self.tmp.name):
            resp = self.client.get(
                f"/api/v1/scrapes/{self.scrape.id}/screenshots/"
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"data": []})

    def test_non_staff_forbidden(self):
        user = User.objects.create_user(username="alice", password="p")
        self.client.force_authenticate(user=user)
        with override_settings(SCREENSHOT_DIR=self.tmp.name):
            resp = self.client.get(
                f"/api/v1/scrapes/{self.scrape.id}/screenshots/"
            )
        self.assertEqual(resp.status_code, 403)
