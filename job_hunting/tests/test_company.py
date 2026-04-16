from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Company

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
