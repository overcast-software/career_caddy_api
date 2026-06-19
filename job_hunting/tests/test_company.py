from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Company, JobApplication, JobPost

User = get_user_model()


class TestCompanyModel(TestCase):
    def test_create_company(self):
        c = Company.objects.create(name="Acme Corp", display_name="Acme")
        self.assertEqual(c.name, "Acme Corp")
        self.assertEqual(c.display_name, "Acme")
        self.assertIsNotNone(c.id)

    def test_str(self):
        c = Company.objects.create(name="Acme Corp", display_name="Acme")
        self.assertEqual(str(c), "Acme")

    def test_str_falls_back_to_name(self):
        c = Company.objects.create(name="Acme Corp")
        self.assertEqual(str(c), "Acme Corp")

    def test_get_or_create(self):
        c1, created = Company.objects.get_or_create(name="TestCo")
        self.assertTrue(created)
        c2, created = Company.objects.get_or_create(name="TestCo")
        self.assertFalse(created)
        self.assertEqual(c1.id, c2.id)


class TestCompanyAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Test Company", display_name="Test Co")

    def test_list_companies(self):
        response = self.client.get("/api/v1/companies/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("data", data)
        ids = [item["id"] for item in data["data"]]
        self.assertIn(str(self.company.id), ids)

    def test_retrieve_company(self):
        response = self.client.get(f"/api/v1/companies/{self.company.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["data"]["id"], str(self.company.id))
        self.assertEqual(data["data"]["attributes"]["name"], "Test Company")

    def test_create_company(self):
        payload = {
            "data": {
                "type": "company",
                "attributes": {"name": "New Corp", "display_name": "New"},
            }
        }
        response = self.client.post(
            "/api/v1/companies/",
            data=payload,
            format="json",
            HTTP_CONTENT_TYPE="application/vnd.api+json",
        )
        self.assertIn(response.status_code, [201, 200])

    def test_delete_company_forbidden_for_non_staff(self):
        c = Company.objects.create(name="ToDelete")
        response = self.client.delete(f"/api/v1/companies/{c.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Company.objects.filter(pk=c.id).exists())

    def test_delete_company_allowed_for_staff(self):
        self.user.is_staff = True
        self.user.save()
        c = Company.objects.create(name="ToDelete")
        response = self.client.delete(f"/api/v1/companies/{c.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Company.objects.filter(pk=c.id).exists())


class TestCompanyListCountsMeta(TestCase):
    """`GET /api/v1/companies/?meta=counts` serves per-company badge
    counts in each resource's JSON:API `meta`; the plain list does not.
    Mirrors the resumes counts-in-meta gate (FRON #115)."""

    _COUNT_KEYS = {
        "job_posts_count",
        "job_applications_count",
        "scrapes_count",
        "questions_count",
        "scores_count",
    }

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="counter", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Counted Co")
        # One job post on the company.
        JobPost.objects.create(
            company=self.company,
            title="Engineer",
            link="https://example.com/jobs/1",
            created_by=self.user,
        )
        # Two applications attached via the DIRECT company FK, with NO
        # job_post — the old job_post__company_id join would miss these,
        # so a count of 2 proves the direct-FK optimization.
        JobApplication.objects.create(company=self.company, user=self.user)
        JobApplication.objects.create(company=self.company, user=self.user)

    def _find_company(self, data):
        return next(
            item for item in data["data"] if item["id"] == str(self.company.id)
        )

    def test_list_with_meta_counts_includes_per_company_counts(self):
        response = self.client.get("/api/v1/companies/?meta=counts")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        resource = self._find_company(response.json())
        self.assertIn("meta", resource)
        self.assertEqual(self._COUNT_KEYS, set(resource["meta"].keys()))
        self.assertEqual(resource["meta"]["job_posts_count"], 1)
        self.assertEqual(resource["meta"]["job_applications_count"], 2)

    def test_list_without_param_omits_counts(self):
        response = self.client.get("/api/v1/companies/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        resource = self._find_company(response.json())
        self.assertNotIn("meta", resource)

    def test_applications_count_uses_direct_company_fk(self):
        """Applications with a company but no job_post still count —
        the count goes through JobApplication.company directly."""
        response = self.client.get("/api/v1/companies/?meta=counts")
        resource = self._find_company(response.json())
        self.assertEqual(resource["meta"]["job_applications_count"], 2)
