from django.test import TestCase
from django.contrib.auth import get_user_model
from job_hunting.models import Profile


class ProfileModelTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="profuser", password="pass")

    def test_create_profile(self):
        profile = Profile.objects.create(user=self.user, phone="555-1234")
        self.assertEqual(profile.phone, "555-1234")
        self.assertEqual(profile.user, self.user)

    def test_one_to_one_intent(self):
        Profile.objects.create(user=self.user, phone="555-0001")
        with self.assertRaises(Exception):
            Profile.objects.create(user=self.user, phone="555-0002")

    def test_links_json(self):
        profile = Profile.objects.create(user=self.user, links={"website": "https://example.com"})
        profile.refresh_from_db()
        self.assertEqual(profile.links["website"], "https://example.com")

    def test_nullable_fields(self):
        profile = Profile.objects.create(user=self.user)
        self.assertIsNone(profile.phone)
        self.assertIsNone(profile.address)
        self.assertTrue(profile.is_active)
