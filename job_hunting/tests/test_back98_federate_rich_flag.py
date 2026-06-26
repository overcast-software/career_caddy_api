"""BACK-98 (Task B) — per-user rich/lean federated-format opt-in flag.

``Profile.federate_rich`` (default False = LEAN), sibling to BACK-91's
``federate_posts``. Asserts the model default, the content-builder gate
reads it, and the user-resource serializer round-trips it.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.lib.as_object import user_opted_into_rich
from job_hunting.models import Profile

User = get_user_model()


class TestFederateRichDefault(TestCase):
    def test_default_is_lean(self):
        user = User.objects.create_user(username="d", password="p")
        prof = Profile.objects.create(user=user)
        self.assertFalse(prof.federate_rich)
        self.assertFalse(user_opted_into_rich(user.id))

    def test_opt_in_reads_rich(self):
        user = User.objects.create_user(username="d2", password="p")
        Profile.objects.create(user=user, federate_rich=True)
        self.assertTrue(user_opted_into_rich(user.id))

    def test_no_profile_is_lean(self):
        user = User.objects.create_user(username="d3", password="p")
        self.assertFalse(user_opted_into_rich(user.id))

    def test_none_user_is_lean(self):
        self.assertFalse(user_opted_into_rich(None))


class TestFederateRichSerializerRoundTrip(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rt", password="p")
        Profile.objects.create(user=self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_user_resource_exposes_federate_rich(self):
        resp = self.client.get(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("federate_rich", attrs)
        self.assertFalse(attrs["federate_rich"])

    def test_patch_sets_federate_rich(self):
        payload = {
            "data": {
                "type": "user",
                "id": str(self.user.id),
                "attributes": {"federate_rich": True},
            }
        }
        resp = self.client.patch(
            f"/api/v1/users/{self.user.id}/", data=payload, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.user.profile_obj.refresh_from_db()
        self.assertTrue(self.user.profile_obj.federate_rich)
        self.assertTrue(resp.json()["data"]["attributes"]["federate_rich"])
