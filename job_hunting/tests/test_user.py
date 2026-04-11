from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

User = get_user_model()


class TestUserSerializer(TestCase):
    """is_staff and is_active are included in the serialized resource."""

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="admin", password="pass", is_staff=True
        )
        self.client.force_authenticate(user=self.staff)

    def test_list_includes_is_staff_and_is_active(self):
        response = self.client.get("/api/v1/users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        attrs = response.json()["data"][0]["attributes"]
        self.assertIn("is_staff", attrs)
        self.assertIn("is_active", attrs)

    def test_retrieve_includes_is_staff_and_is_active(self):
        response = self.client.get(f"/api/v1/users/{self.staff.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        attrs = response.json()["data"]["attributes"]
        self.assertTrue(attrs["is_staff"])
        self.assertTrue(attrs["is_active"])


class TestUserListStaffGate(TestCase):
    """Non-staff users only see themselves; staff see all."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ordinary", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.staff = User.objects.create_user(
            username="staffperson", password="pass", is_staff=True
        )

    def test_non_staff_list_returns_only_self(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in response.json()["data"]]
        self.assertEqual(ids, [str(self.user.id)])

    def test_staff_list_returns_all(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get("/api/v1/users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in response.json()["data"]]
        self.assertIn(str(self.user.id), ids)
        self.assertIn(str(self.other.id), ids)
        self.assertIn(str(self.staff.id), ids)


class TestUserRetrieveStaffGate(TestCase):
    """Staff can retrieve any user; non-staff cannot retrieve others."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="me", password="pass")
        self.other = User.objects.create_user(username="them", password="pass")
        self.staff = User.objects.create_user(
            username="staffone", password="pass", is_staff=True
        )

    def test_non_staff_cannot_retrieve_other_user(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(f"/api/v1/users/{self.other.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_staff_can_retrieve_self(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_retrieve_any_user(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f"/api/v1/users/{self.other.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["data"]["id"], str(self.other.id))


class TestUserIsStaffToggle(TestCase):
    """Staff can promote/demote users; non-staff cannot."""

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="stafftwo", password="pass", is_staff=True
        )
        self.target = User.objects.create_user(username="target", password="pass")

    def test_staff_can_promote_user(self):
        self.client.force_authenticate(user=self.staff)
        payload = {"data": {"type": "user", "attributes": {"is-staff": True}}}
        response = self.client.patch(
            f"/api/v1/users/{self.target.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_staff)
        self.assertTrue(response.json()["data"]["attributes"]["is_staff"])

    def test_staff_can_demote_user(self):
        self.target.is_staff = True
        self.target.save()
        self.client.force_authenticate(user=self.staff)
        payload = {"data": {"type": "user", "attributes": {"is-staff": False}}}
        response = self.client.patch(
            f"/api/v1/users/{self.target.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_staff)

    def test_non_staff_cannot_set_is_staff(self):
        self.client.force_authenticate(user=self.target)
        payload = {"data": {"type": "user", "attributes": {"is-staff": True}}}
        response = self.client.patch(
            f"/api/v1/users/{self.target.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_staff)


class TestUserIsActiveToggle(TestCase):
    """Staff can activate/deactivate users; non-staff cannot."""

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staffthree", password="pass", is_staff=True
        )
        self.target = User.objects.create_user(username="activetarget", password="pass")

    def test_staff_can_deactivate_user(self):
        self.client.force_authenticate(user=self.staff)
        payload = {"data": {"type": "user", "attributes": {"is-active": False}}}
        response = self.client.patch(
            f"/api/v1/users/{self.target.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        self.assertFalse(response.json()["data"]["attributes"]["is_active"])

    def test_staff_can_reactivate_user(self):
        self.target.is_active = False
        self.target.save()
        self.client.force_authenticate(user=self.staff)
        payload = {"data": {"type": "user", "attributes": {"is-active": True}}}
        response = self.client.patch(
            f"/api/v1/users/{self.target.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_non_staff_cannot_set_is_active(self):
        self.client.force_authenticate(user=self.target)
        payload = {"data": {"type": "user", "attributes": {"is-active": False}}}
        response = self.client.patch(
            f"/api/v1/users/{self.target.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)


class TestUserDestroy(TestCase):
    """Only staff can delete users."""

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staffdel", password="pass", is_staff=True
        )
        self.user = User.objects.create_user(username="todelete", password="pass")

    def test_non_staff_cannot_delete_user(self):
        requester = User.objects.create_user(username="nonstaffdel", password="pass")
        self.client.force_authenticate(user=requester)
        response = self.client.delete(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(User.objects.filter(id=self.user.id).exists())

    def test_staff_can_delete_user(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.delete(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(id=self.user.id).exists())


class TestUserCreate(TestCase):
    """Staff can create users via POST /api/v1/users/."""

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staffcreate", password="pass", is_staff=True
        )

    def test_staff_can_create_user(self):
        self.client.force_authenticate(user=self.staff)
        payload = {
            "data": {
                "type": "user",
                "attributes": {
                    "username": "newuser",
                    "email": "new@example.com",
                    "password": "securepass123",
                    "first_name": "New",
                    "last_name": "User",
                },
            }
        }
        response = self.client.post("/api/v1/users/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(username="newuser").exists())

    def test_duplicate_username_returns_400(self):
        self.client.force_authenticate(user=self.staff)
        payload = {
            "data": {
                "type": "user",
                "attributes": {"username": "staffcreate", "password": "pass"},
            }
        }
        response = self.client.post("/api/v1/users/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
