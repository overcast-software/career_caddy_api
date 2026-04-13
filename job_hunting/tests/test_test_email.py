from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

User = get_user_model()


class TestTestEmail(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admin", email="admin@example.com", password="AdminPass123!",
            is_staff=True,
        )
        self.regular = User.objects.create_user(
            username="regular", email="regular@example.com", password="RegPass123!",
        )

    def test_admin_sends_test_email(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/v1/test-email/",
            {"email": "target@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["target@example.com"])
        self.assertIn("Test Email", mail.outbox[0].subject)

    def test_default_to_own_email(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/api/v1/test-email/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["admin@example.com"])

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.regular)
        response = self.client.post(
            "/api/v1/test-email/",
            {"email": "target@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_forbidden(self):
        response = self.client.post(
            "/api/v1/test-email/",
            {"email": "target@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
