import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Profile

User = get_user_model()


class ProfileOnboardingShapeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="awuser", password="pass")

    def test_default_shape_has_all_keys(self):
        shape = Profile.default_onboarding()
        self.assertEqual(
            set(shape.keys()),
            {
                "wizard_enabled",
                "profile_basics",
                "resume_imported",
                "resume_reviewed",
                "first_job_post",
                "first_score",
                "first_cover_letter",
            },
        )
        self.assertTrue(shape["wizard_enabled"])
        for k in shape:
            if k != "wizard_enabled":
                self.assertFalse(shape[k])

    def test_resolved_onboarding_fills_defaults(self):
        prof = Profile.objects.create(user=self.user, onboarding={"resume_imported": True})
        resolved = prof.resolved_onboarding()
        self.assertTrue(resolved["resume_imported"])
        self.assertTrue(resolved["wizard_enabled"])
        self.assertFalse(resolved["resume_reviewed"])

    def test_merge_preserves_untouched_keys(self):
        prof = Profile.objects.create(
            user=self.user,
            onboarding={"resume_imported": True, "first_job_post": True},
        )
        prof.merge_onboarding({"resume_reviewed": True})
        prof.save()
        prof.refresh_from_db()
        resolved = prof.resolved_onboarding()
        self.assertTrue(resolved["resume_imported"])
        self.assertTrue(resolved["first_job_post"])
        self.assertTrue(resolved["resume_reviewed"])

    def test_merge_ignores_unknown_keys(self):
        prof = Profile.objects.create(user=self.user)
        prof.merge_onboarding({"bogus_key": True, "resume_imported": True})
        prof.save()
        prof.refresh_from_db()
        self.assertNotIn("bogus_key", prof.onboarding)
        self.assertTrue(prof.onboarding["resume_imported"])

    def test_merge_coerces_to_bool(self):
        prof = Profile.objects.create(user=self.user)
        prof.merge_onboarding({"wizard_enabled": 0, "resume_imported": "yes"})
        self.assertEqual(prof.onboarding["wizard_enabled"], False)
        self.assertEqual(prof.onboarding["resume_imported"], True)


class UserSerializerOnboardingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="awuser", password="pass")
        self.client.force_authenticate(user=self.user)

    def test_retrieve_includes_default_onboarding_when_profile_absent(self):
        resp = self.client.get(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("onboarding", attrs)
        self.assertTrue(attrs["onboarding"]["wizard_enabled"])
        self.assertFalse(attrs["onboarding"]["resume_imported"])

    def test_retrieve_reflects_stored_onboarding(self):
        Profile.objects.create(user=self.user, onboarding={"resume_imported": True})
        resp = self.client.get(f"/api/v1/users/{self.user.id}/")
        attrs = resp.json()["data"]["attributes"]
        self.assertTrue(attrs["onboarding"]["resume_imported"])
        self.assertTrue(attrs["onboarding"]["wizard_enabled"])

    def test_partial_patch_merges_onboarding(self):
        Profile.objects.create(
            user=self.user,
            onboarding={"resume_imported": True, "first_job_post": True},
        )
        payload = {
            "data": {
                "type": "user",
                "id": str(self.user.id),
                "attributes": {"onboarding": {"resume_reviewed": True}},
            }
        }
        resp = self.client.patch(
            f"/api/v1/users/{self.user.id}/",
            data=json.dumps(payload),
            content_type="application/vnd.api+json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        attrs = resp.json()["data"]["attributes"]
        onb = attrs["onboarding"]
        self.assertTrue(onb["resume_imported"])
        self.assertTrue(onb["first_job_post"])
        self.assertTrue(onb["resume_reviewed"])

    def test_patch_wizard_disable(self):
        Profile.objects.create(user=self.user)
        payload = {
            "data": {
                "type": "user",
                "id": str(self.user.id),
                "attributes": {"onboarding": {"wizard_enabled": False}},
            }
        }
        resp = self.client.patch(
            f"/api/v1/users/{self.user.id}/",
            data=json.dumps(payload),
            content_type="application/vnd.api+json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        attrs = resp.json()["data"]["attributes"]
        self.assertFalse(attrs["onboarding"]["wizard_enabled"])
