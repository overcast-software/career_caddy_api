from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import mail
from django.test import TestCase
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient
from rest_framework import status

User = get_user_model()


class TestPasswordResetRequest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="OldPassword123!",
        )

    def test_valid_email_sends_reset(self):
        response = self.client.post(
            "/api/v1/password-reset/",
            {"email": "test@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset-password", mail.outbox[0].body)
        self.assertIn("token=", mail.outbox[0].body)
        self.assertIn("&uid=", mail.outbox[0].body)
        self.assertNotIn("&amp;", mail.outbox[0].body)

    def test_unknown_email_still_200(self):
        response = self.client.post(
            "/api/v1/password-reset/",
            {"email": "nobody@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_email_400(self):
        response = self.client.post(
            "/api/v1/password-reset/", {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_email_format_400(self):
        response = self.client.post(
            "/api/v1/password-reset/",
            {"email": "notanemail"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_not_allowed(self):
        response = self.client.get("/api/v1/password-reset/")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_case_insensitive_email(self):
        response = self.client.post(
            "/api/v1/password-reset/",
            {"email": "TEST@EXAMPLE.COM"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)


class TestPasswordResetConfirm(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="OldPassword123!",
        )
        self.token_generator = PasswordResetTokenGenerator()
        self.token = self.token_generator.make_token(self.user)
        self.uid = urlsafe_base64_encode(force_bytes(self.user.pk))

    def test_valid_reset(self):
        response = self.client.post(
            "/api/v1/password-reset/confirm/",
            {
                "token": self.token,
                "uid": self.uid,
                "new_password": "NewSecurePass456!",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewSecurePass456!"))

    def test_invalid_token(self):
        response = self.client.post(
            "/api/v1/password-reset/confirm/",
            {
                "token": "bad-token",
                "uid": self.uid,
                "new_password": "NewSecurePass456!",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_uid(self):
        response = self.client.post(
            "/api/v1/password-reset/confirm/",
            {
                "token": self.token,
                "uid": "baduid",
                "new_password": "NewSecurePass456!",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_token_reuse_after_password_change(self):
        self.client.post(
            "/api/v1/password-reset/confirm/",
            {
                "token": self.token,
                "uid": self.uid,
                "new_password": "NewSecurePass456!",
            },
            format="json",
        )
        response = self.client.post(
            "/api/v1/password-reset/confirm/",
            {
                "token": self.token,
                "uid": self.uid,
                "new_password": "AnotherPass789!",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_fields(self):
        response = self.client.post(
            "/api/v1/password-reset/confirm/",
            {"token": self.token},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_password(self):
        response = self.client.post(
            "/api/v1/password-reset/confirm/",
            {
                "token": self.token,
                "uid": self.uid,
                "new_password": "",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_not_allowed(self):
        response = self.client.get("/api/v1/password-reset/confirm/")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class TestWaitlistConfirmationEmail(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_waitlist_signup_sends_confirmation_email(self):
        response = self.client.post(
            "/api/v1/waitlist/",
            {"email": "waitlist@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("waiting list", mail.outbox[0].subject.lower())


class TestWelcomeEmail(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_registration_sends_welcome_email(self):
        response = self.client.post(
            "/api/v1/auth/register/",
            {
                "data": {
                    "type": "users",
                    "attributes": {
                        "username": "newuser",
                        "email": "new@example.com",
                        "password": "SecurePass123!",
                    },
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Welcome to Career Caddy")
        self.assertIn("newuser", mail.outbox[0].body)
