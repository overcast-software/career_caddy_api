"""Tests for GET /api/v1/scrape-profiles/ list filters.

ScrapeProfileViewSet.list supports two filters that compose:

- filter[hostname] — exact match (legacy)
- filter[query]    — icontains on hostname (drives the admin
                     /admin/scrape-profiles/index infinite-scroll
                     search box)

Both apply BEFORE pagination so meta.total reflects the filtered set.
The list endpoint is gated by IsAdminUser, so all tests authenticate as
a staff user.
"""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import ScrapeProfile

User = get_user_model()


class TestScrapeProfileListFilterQuery(TestCase):
    URL = "/api/v1/scrape-profiles/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="admin-listfilter", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.user)
        # Wipe any data-migration-seeded rows so total/page assertions
        # are deterministic regardless of what 0076/0077/0093 etc. seed.
        ScrapeProfile.objects.all().delete()

    def _create(self, hostname):
        return ScrapeProfile.objects.create(hostname=hostname)

    def test_filter_query_icontains_hostname(self):
        """filter[query]=<substring> returns rows whose hostname
        icontains the substring."""
        self._create("linkedin.com")
        self._create("jobs.linkedin.com")
        self._create("example.com")
        self._create("greenhouse.io")

        resp = self.client.get(self.URL + "?filter[query]=linkedin")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        hostnames = {item["attributes"]["hostname"] for item in body["data"]}
        self.assertEqual(hostnames, {"linkedin.com", "jobs.linkedin.com"})
        self.assertEqual(body["meta"]["total"], 2)

    def test_filter_query_case_insensitive(self):
        """Match is case-insensitive (icontains)."""
        self._create("LinkedIn.com")
        self._create("example.com")

        resp = self.client.get(self.URL + "?filter[query]=linkedin")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["meta"]["total"], 1)
        self.assertEqual(
            body["data"][0]["attributes"]["hostname"], "LinkedIn.com"
        )

    def test_filter_query_paginates_filtered_set(self):
        """Filter applies BEFORE pagination — meta.total reflects only
        matching rows, and per_page slices the filtered set."""
        for i in range(5):
            self._create(f"jobs{i}.linkedin.com")
        self._create("example.com")  # excluded by filter

        resp = self.client.get(
            self.URL + "?filter[query]=linkedin&per_page=2&page=1"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["meta"]["total"], 5)
        self.assertEqual(body["meta"]["per_page"], 2)
        self.assertEqual(body["meta"]["page"], 1)
        self.assertEqual(body["meta"]["total_pages"], 3)
        self.assertEqual(len(body["data"]), 2)

        resp3 = self.client.get(
            self.URL + "?filter[query]=linkedin&per_page=2&page=3"
        )
        body3 = resp3.json()
        self.assertEqual(body3["meta"]["total"], 5)
        self.assertEqual(body3["meta"]["page"], 3)
        self.assertEqual(len(body3["data"]), 1)  # last partial page

    def test_filter_query_combines_with_filter_hostname(self):
        """Both filters set → intersect. filter[hostname] is exact; if
        the query substring matches but the exact hostname doesn't,
        zero results."""
        self._create("linkedin.com")
        self._create("jobs.linkedin.com")

        # Intersect hit: both filters point at the same row.
        resp = self.client.get(
            self.URL + "?filter[hostname]=linkedin.com&filter[query]=linkedin"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["meta"]["total"], 1)
        self.assertEqual(
            body["data"][0]["attributes"]["hostname"], "linkedin.com"
        )

        # Intersect miss: exact hostname doesn't exist even though the
        # substring matches another row.
        resp_miss = self.client.get(
            self.URL
            + "?filter[hostname]=does-not-exist.com&filter[query]=linkedin"
        )
        self.assertEqual(resp_miss.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_miss.json()["meta"]["total"], 0)

    def test_missing_filter_query_unchanged_behavior(self):
        """Without filter[query], list returns all rows (subject only
        to filter[hostname] if set)."""
        self._create("linkedin.com")
        self._create("example.com")
        self._create("greenhouse.io")

        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["meta"]["total"], 3)
        hostnames = {item["attributes"]["hostname"] for item in body["data"]}
        self.assertEqual(
            hostnames, {"linkedin.com", "example.com", "greenhouse.io"}
        )
