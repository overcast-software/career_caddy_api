import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    CoverLetter,
    JobPost,
    Profile,
    Resume,
    Score,
)

User = get_user_model()


class ReconcileOnboardingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="existing",
            password="pass",
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
        )
        self.client.force_authenticate(user=self.user)

    def _post(self):
        return self.client.post(
            "/api/v1/onboarding/reconcile/",
            data=json.dumps({}),
            content_type="application/vnd.api+json",
        )

    def test_fresh_user_with_no_data_stays_mostly_false(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()["data"]
        self.assertTrue(body["profile_basics"])  # name+email set in setUp
        self.assertFalse(body["resume_imported"])
        self.assertFalse(body["first_job_post"])
        self.assertFalse(body["first_score"])
        self.assertFalse(body["first_cover_letter"])
        self.assertTrue(body["wizard_enabled"])
        self.assertFalse(body["resume_reviewed"])

    def test_existing_resume_flips_resume_imported(self):
        Resume.objects.create(user=self.user, title="SRE")
        resp = self._post()
        body = resp.json()["data"]
        self.assertTrue(body["resume_imported"])

    def test_existing_job_post_flips_first_job_post(self):
        company = Company.objects.create(name="Acme")
        JobPost.objects.create(
            title="Backend Eng",
            company=company,
            created_by=self.user,
        )
        resp = self._post()
        body = resp.json()["data"]
        self.assertTrue(body["first_job_post"])

    def test_reconcile_preserves_resume_reviewed_and_wizard_enabled(self):
        """Subjective fields stay as stored — reconcile never blanks them."""
        Profile.objects.create(
            user=self.user,
            onboarding={"resume_reviewed": True, "wizard_enabled": False},
        )
        Resume.objects.create(user=self.user, title="SRE")
        resp = self._post()
        body = resp.json()["data"]
        self.assertTrue(body["resume_imported"])  # derived — now true
        self.assertTrue(body["resume_reviewed"])  # preserved
        self.assertFalse(body["wizard_enabled"])  # preserved

    def test_reconcile_flips_from_stale_false_to_true(self):
        """The motivating case: long-time user whose blob defaults to false."""
        Profile.objects.create(
            user=self.user,
            onboarding={
                "resume_imported": False,
                "first_job_post": False,
                "first_score": False,
                "first_cover_letter": False,
            },
        )
        Resume.objects.create(user=self.user, title="SRE")
        company = Company.objects.create(name="Acme")
        post = JobPost.objects.create(
            title="Eng", company=company, created_by=self.user
        )
        Score.objects.create(user=self.user, job_post=post)
        CoverLetter.objects.create(user=self.user, content="Dear hiring...")

        resp = self._post()
        body = resp.json()["data"]
        self.assertTrue(body["resume_imported"])
        self.assertTrue(body["first_job_post"])
        self.assertTrue(body["first_score"])
        self.assertTrue(body["first_cover_letter"])

    def test_reconcile_persists_to_profile(self):
        Resume.objects.create(user=self.user, title="SRE")
        self._post()
        prof = Profile.objects.get(user_id=self.user.id)
        self.assertTrue(prof.onboarding["resume_imported"])

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self._post()
        self.assertIn(resp.status_code, (401, 403))

    def test_only_affects_caller(self):
        """Reconcile should never touch another user's data."""
        other = User.objects.create_user(username="other", password="pass")
        Resume.objects.create(user=other, title="Other Resume")
        resp = self._post()
        body = resp.json()["data"]
        # My reconcile sees MY data (none), not other's.
        self.assertFalse(body["resume_imported"])
