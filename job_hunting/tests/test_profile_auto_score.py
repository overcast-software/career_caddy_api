from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Profile


User = get_user_model()


class TestProfileAutoScoreField(TestCase):
    """Profile.auto_score is the per-user opt-in for the auto-score daemon.
    Default off; exposed on the user serializer; writable via PATCH."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="as", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_default_false_and_readable(self):
        resp = self.client.get(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("auto_score", attrs)
        self.assertFalse(attrs["auto_score"])

    def test_patch_sets_auto_score_true(self):
        payload = {
            "data": {
                "type": "user",
                "id": str(self.user.id),
                "attributes": {"auto_score": True},
            }
        }
        resp = self.client.patch(
            f"/api/v1/users/{self.user.id}/",
            data=payload,
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertTrue(resp.json()["data"]["attributes"]["auto_score"])

        prof = Profile.objects.get(user_id=self.user.id)
        self.assertTrue(prof.auto_score)
