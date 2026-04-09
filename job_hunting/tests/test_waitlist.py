from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Waitlist


class TestWaitlistModel(TestCase):
    def test_create(self):
        w = Waitlist.objects.create(email="test@example.com")
        self.assertEqual(w.email, "test@example.com")
        self.assertIsNotNone(w.created_at)

    def test_str(self):
        w = Waitlist.objects.create(email="test@example.com")
        self.assertEqual(str(w), "Waitlist(test@example.com)")

    def test_unique_email(self):
        Waitlist.objects.create(email="test@example.com")
        with self.assertRaises(Exception):
            Waitlist.objects.create(email="test@example.com")


class TestWaitlistAPI(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_signup_success(self):
        response = self.client.post(
            "/api/v1/waitlist/",
            {"email": "new@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Waitlist.objects.filter(email="new@example.com").exists())

    def test_signup_missing_email(self):
        response = self.client.post("/api/v1/waitlist/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_invalid_email(self):
        response = self.client.post(
            "/api/v1/waitlist/",
            {"email": "notanemail"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_duplicate(self):
        Waitlist.objects.create(email="dup@example.com")
        response = self.client.post(
            "/api/v1/waitlist/",
            {"email": "dup@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_get_not_allowed(self):
        response = self.client.get("/api/v1/waitlist/")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_no_auth_required(self):
        # No force_authenticate — should still work
        response = self.client.post(
            "/api/v1/waitlist/",
            {"email": "anon@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
