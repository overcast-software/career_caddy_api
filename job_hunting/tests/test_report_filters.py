from datetime import timedelta

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost


User = get_user_model()
FLOW = "/api/v1/reports/application-flow/"
SOURCES = "/api/v1/reports/sources/"
OPTS = "/api/v1/reports/filter-options/"


class TestReportFilters(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ru", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def _post(self, **kwargs):
        return JobPost.objects.create(
            title=kwargs.pop("title", "T"),
            company=self.company,
            created_by=self.user,
            **kwargs,
        )

    def _attrs(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()["data"]["attributes"]

    def test_source_filter_narrows_counts(self):
        self._post(title="Email 1", source="email")
        self._post(title="Email 2", source="email")
        self._post(title="Paste 1", source="paste")
        all_attrs = self._attrs(self.client.get(FLOW))
        self.assertEqual(all_attrs["total_job_posts"], 3)
        email_attrs = self._attrs(self.client.get(FLOW + "?source=email"))
        self.assertEqual(email_attrs["total_job_posts"], 2)
        paste_attrs = self._attrs(self.client.get(FLOW + "?source=paste"))
        self.assertEqual(paste_attrs["total_job_posts"], 1)

    def test_date_from_filter(self):
        old = self._post(title="Old")
        JobPost.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=100)
        )
        self._post(title="Recent")
        cutoff = (timezone.now() - timedelta(days=30)).date().isoformat()
        attrs = self._attrs(self.client.get(FLOW + f"?from={cutoff}"))
        self.assertEqual(attrs["total_job_posts"], 1)

    def test_date_to_filter_is_inclusive_of_day(self):
        p = self._post(title="End of day")
        # Put the row at 23:59 UTC today.
        today = timezone.now().replace(hour=23, minute=59, second=0, microsecond=0)
        JobPost.objects.filter(pk=p.pk).update(created_at=today)
        cutoff = today.date().isoformat()
        attrs = self._attrs(self.client.get(FLOW + f"?to={cutoff}"))
        self.assertEqual(attrs["total_job_posts"], 1)

    def test_user_filter_staff_only(self):
        other = User.objects.create_user(username="other", password="pw")
        JobPost.objects.create(
            title="Theirs", company=self.company, created_by=other
        )
        # Non-staff user using ?user= → 403.
        r = self.client.get(FLOW + "?scope=all&user={}".format(other.id))
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_filter_as_staff_narrows_scope(self):
        staff = User.objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        other = User.objects.create_user(username="other", password="pw")
        JobPost.objects.create(
            title="Theirs", company=self.company, created_by=other
        )
        self._post(title="Mine")
        self.client.force_authenticate(user=staff)
        r = self.client.get(FLOW + "?scope=all&user={}".format(other.id))
        attrs = self._attrs(r)
        self.assertEqual(attrs["total_job_posts"], 1)

    def test_sources_report_honors_filters(self):
        # Prove filters flow through to the other endpoint too.
        self._post(title="Em", source="email")
        self._post(title="Ma", source="manual")
        attrs = self._attrs(self.client.get(SOURCES + "?source=email"))
        self.assertEqual(attrs["total_job_posts"], 1)

    def test_filter_options_lists_known_sources(self):
        self._post(title="E", source="email")
        resp = self.client.get(OPTS)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn("sources", data)
        self.assertIn("email", data["sources"])
        # Non-staff response does not include the users list.
        self.assertNotIn("users", data)

    def test_filter_options_includes_users_for_staff(self):
        staff = User.objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=staff)
        resp = self.client.get(OPTS)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn("users", data)
        self.assertTrue(any(u["id"] == staff.id for u in data["users"]))
