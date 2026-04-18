"""Ownership gate on DjangoUserViewSet PATCH/PUT.

Regression guard for the vulnerability found during Agent Wizard review:
_upsert previously had no ownership check, letting any authenticated user
overwrite another account's password/email/profile fields.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class UserUpsertOwnershipTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.alice = User.objects.create_user(username="alice", password="a-pass")
        self.bob = User.objects.create_user(username="bob", password="b-pass")
        self.staff = User.objects.create_user(
            username="staffer", password="s-pass", is_staff=True
        )

    def _patch(self, target_id, attributes):
        payload = {
            "data": {
                "type": "user",
                "id": str(target_id),
                "attributes": attributes,
            }
        }
        return self.client.patch(
            f"/api/v1/users/{target_id}/",
            data=json.dumps(payload),
            content_type="application/vnd.api+json",
        )

    def test_non_staff_can_patch_self(self):
        self.client.force_authenticate(user=self.alice)
        resp = self._patch(self.alice.id, {"phone": "555-1111"})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_non_staff_cannot_patch_other_user(self):
        self.client.force_authenticate(user=self.alice)
        resp = self._patch(self.bob.id, {"phone": "555-9999"})
        self.assertEqual(resp.status_code, 403, resp.content)
        self.bob.refresh_from_db()
        # Side-effect must not have landed.
        from job_hunting.models import Profile

        prof = Profile.objects.filter(user_id=self.bob.id).first()
        self.assertFalse(prof and prof.phone == "555-9999")

    def test_non_staff_cannot_change_other_users_password(self):
        """The account-takeover path that motivated this fix."""
        self.client.force_authenticate(user=self.alice)
        resp = self._patch(self.bob.id, {"password": "hijacked-pass"})
        self.assertEqual(resp.status_code, 403, resp.content)
        # Bob's original password must still work.
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.check_password("b-pass"))

    def test_non_staff_cannot_patch_other_users_onboarding(self):
        self.client.force_authenticate(user=self.alice)
        resp = self._patch(self.bob.id, {"onboarding": {"wizard_enabled": False}})
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_staff_can_patch_other_user(self):
        self.client.force_authenticate(user=self.staff)
        resp = self._patch(self.bob.id, {"phone": "555-2222"})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_put_has_same_guard(self):
        """PUT (update) shares _upsert; must enforce the same ownership rule."""
        self.client.force_authenticate(user=self.alice)
        payload = {
            "data": {
                "type": "user",
                "id": str(self.bob.id),
                "attributes": {"email": "pwn@example.com"},
            }
        }
        resp = self.client.put(
            f"/api/v1/users/{self.bob.id}/",
            data=json.dumps(payload),
            content_type="application/vnd.api+json",
        )
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_missing_user_still_404_not_403(self):
        """Shape of the response for truly-missing users should remain 404."""
        self.client.force_authenticate(user=self.alice)
        resp = self._patch(999999, {"phone": "555-0000"})
        self.assertEqual(resp.status_code, 404, resp.content)
