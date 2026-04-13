import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.models import Invitation, Waitlist

User = get_user_model()


class TestInvitationModel(TestCase):
    def test_create(self):
        inv = Invitation.objects.create(
            email="test@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(inv.email, "test@example.com")
        self.assertIsNotNone(inv.created_at)
        self.assertIsNone(inv.accepted_at)

    def test_str(self):
        inv = Invitation.objects.create(
            email="test@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(str(inv), "Invitation(test@example.com)")

    def test_is_expired(self):
        inv = Invitation.objects.create(
            email="test@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() - timedelta(hours=1),
        )
        self.assertTrue(inv.is_expired)

    def test_is_valid(self):
        inv = Invitation.objects.create(
            email="test@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertTrue(inv.is_valid)
        self.assertFalse(inv.is_expired)
        self.assertFalse(inv.is_accepted)

    def test_is_accepted(self):
        inv = Invitation.objects.create(
            email="test@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
            accepted_at=timezone.now(),
        )
        self.assertTrue(inv.is_accepted)
        self.assertFalse(inv.is_valid)


class TestCreateInvitation(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admin", email="admin@example.com", password="AdminPass123!",
            is_staff=True,
        )
        self.regular = User.objects.create_user(
            username="regular", email="regular@example.com", password="RegPass123!",
        )

    def test_create_sends_email(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/v1/invitations/",
            {"data": {"type": "invitations", "attributes": {"email": "invitee@example.com"}}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("invited", mail.outbox[0].subject.lower())
        self.assertIn("accept-invite", mail.outbox[0].body)
        self.assertIn("token=", mail.outbox[0].body)
        inv = Invitation.objects.get(email="invitee@example.com")
        self.assertEqual(inv.created_by, self.admin)
        self.assertIsNotNone(inv.expires_at)

    def test_create_removes_waitlist_entry(self):
        Waitlist.objects.create(email="waitlisted@example.com")
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/v1/invitations/",
            {"data": {"type": "invitations", "attributes": {"email": "waitlisted@example.com"}}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(Waitlist.objects.filter(email="waitlisted@example.com").exists())

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.regular)
        response = self.client.post(
            "/api/v1/invitations/",
            {"data": {"type": "invitations", "attributes": {"email": "invitee@example.com"}}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_forbidden(self):
        response = self.client.post(
            "/api/v1/invitations/",
            {"data": {"type": "invitations", "attributes": {"email": "invitee@example.com"}}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_email_400(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/v1/invitations/",
            {"data": {"type": "invitations", "attributes": {}}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TestAcceptInvite(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.token = secrets.token_urlsafe(32)
        self.invitation = Invitation.objects.create(
            email="invitee@example.com",
            token=self.token,
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_valid_accept(self):
        response = self.client.post(
            "/api/v1/accept-invite/",
            {
                "token": self.token,
                "username": "newuser",
                "password": "SecurePass123!",
                "first_name": "New",
                "last_name": "User",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(username="newuser")
        self.assertEqual(user.email, "invitee@example.com")
        self.assertEqual(user.first_name, "New")
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.accepted_at)

    def test_expired_token(self):
        self.invitation.expires_at = timezone.now() - timedelta(hours=1)
        self.invitation.save()
        response = self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token, "username": "newuser", "password": "SecurePass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_already_accepted(self):
        self.invitation.accepted_at = timezone.now()
        self.invitation.save()
        response = self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token, "username": "newuser", "password": "SecurePass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_token(self):
        response = self.client.post(
            "/api/v1/accept-invite/",
            {"token": "bogus-token", "username": "newuser", "password": "SecurePass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_fields(self):
        response = self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_username(self):
        User.objects.create_user(username="taken", password="Pass123!")
        response = self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token, "username": "taken", "password": "SecurePass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["errors"][0]["detail"])

    def test_weak_password(self):
        response = self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token, "username": "newuser", "password": "123"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_welcome_email_sent(self):
        self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token, "username": "newuser", "password": "SecurePass123!"},
            format="json",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Welcome to Career Caddy")

    def test_profile_created(self):
        self.client.post(
            "/api/v1/accept-invite/",
            {"token": self.token, "username": "newuser", "password": "SecurePass123!"},
            format="json",
        )
        from job_hunting.models import Profile
        user = User.objects.get(username="newuser")
        self.assertTrue(Profile.objects.filter(user=user).exists())


class TestListInvitations(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admin", email="admin@example.com", password="AdminPass123!",
            is_staff=True,
        )
        self.regular = User.objects.create_user(
            username="regular", password="RegPass123!",
        )
        Invitation.objects.create(
            email="a@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_admin_can_list(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/invitations/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["data"]), 1)

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.regular)
        response = self.client.get("/api/v1/invitations/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TestDestroyInvitation(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admin", email="admin@example.com", password="AdminPass123!",
            is_staff=True,
        )
        self.regular = User.objects.create_user(
            username="regular", password="RegPass123!",
        )
        self.invitation = Invitation.objects.create(
            email="revoke@example.com",
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_admin_can_revoke(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(f"/api/v1/invitations/{self.invitation.pk}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Invitation.objects.filter(pk=self.invitation.pk).exists())

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.regular)
        response = self.client.delete(f"/api/v1/invitations/{self.invitation.pk}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
