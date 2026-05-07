"""GET + PATCH /api/v1/onboarding/

GET: read-only fetch in split-shape ({derived, subjective}). Must not mutate
the stored blob.

PATCH: strict subjective-only update. Returns 400 on derived or unknown
keys; the api refuses to pretend a write to a derived flag took. Use POST
/api/v1/onboarding/reconcile/ to refresh derived flags.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Profile, Resume

User = get_user_model()


class OnboardingGetTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="u1",
            password="p",
            first_name="Jane",
            last_name="Doe",
            email="jane@example.com",
        )
        self.client.force_authenticate(user=self.user)

    def test_returns_jsonapi_split_shape(self):
        resp = self.client.get("/api/v1/users/me/onboarding/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        # JSON:API resource shape — type, id, attributes
        self.assertEqual(body["type"], "onboarding")
        self.assertEqual(body["id"], str(self.user.id))
        attrs = body["attributes"]
        self.assertEqual(set(attrs.keys()), {"derived", "subjective"})
        self.assertEqual(
            set(attrs["derived"].keys()),
            {"profile_basics", "resume_imported", "first_job_post",
             "first_score", "first_cover_letter"},
        )
        self.assertEqual(
            set(attrs["subjective"].keys()),
            {"wizard_enabled", "resume_reviewed"},
        )

    def test_get_does_not_mutate_stored_blob(self):
        # Reconcile would flip resume_imported true if the resume is present;
        # GET must not. Stored stays whatever was there.
        Profile.objects.create(user=self.user, onboarding={"resume_imported": False})
        Resume.objects.create(user=self.user, title="SRE")

        # Sanity: GET reports the stored value (false), not a recomputed value.
        resp = self.client.get("/api/v1/users/me/onboarding/")
        self.assertFalse(resp.json()["data"]["attributes"]["derived"]["resume_imported"])

        prof = Profile.objects.get(user_id=self.user.id)
        self.assertEqual(prof.onboarding["resume_imported"], False)

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/v1/users/me/onboarding/")
        self.assertIn(resp.status_code, (401, 403))


class OnboardingPatchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="u1", password="p")
        self.client.force_authenticate(user=self.user)

    def _patch(self, body):
        return self.client.patch(
            "/api/v1/users/me/onboarding/",
            data=json.dumps(body),
            content_type="application/vnd.api+json",
        )

    def test_subjective_key_accepted(self):
        resp = self._patch({"resume_reviewed": True})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["data"]["attributes"]["subjective"]["resume_reviewed"])

        prof = Profile.objects.get(user_id=self.user.id)
        self.assertTrue(prof.onboarding["resume_reviewed"])

    def test_subjective_key_via_jsonapi_attributes(self):
        # Flat keys nested under data.attributes (manual / agent path).
        resp = self._patch({
            "data": {"attributes": {"wizard_enabled": False}},
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        attrs = resp.json()["data"]["attributes"]
        self.assertFalse(attrs["subjective"]["wizard_enabled"])

    def test_subjective_key_via_ember_data_payload(self):
        # Ember Data PATCH shape: data.attributes.subjective is the changed
        # model attribute. Server unwraps the inner dict.
        resp = self._patch({
            "data": {
                "type": "onboarding",
                "id": str(self.user.id),
                "attributes": {"subjective": {"wizard_enabled": False}},
            },
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        attrs = resp.json()["data"]["attributes"]
        self.assertFalse(attrs["subjective"]["wizard_enabled"])

    def test_derived_key_rejected_with_400(self):
        resp = self._patch({"resume_imported": True})
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertIn("resume_imported", body["errors"][0]["detail"])

    def test_unknown_key_rejected_with_400(self):
        resp = self._patch({"bogus_key": True})
        self.assertEqual(resp.status_code, 400)

    def test_mixed_subjective_and_derived_rejects_whole_patch(self):
        # Atomicity: a patch containing one derived key 400s; nothing
        # subjective in the same patch is applied.
        resp = self._patch({"resume_reviewed": True, "first_score": True})
        self.assertEqual(resp.status_code, 400)
        # resume_reviewed should NOT be applied — atomic reject.
        prof = Profile.objects.filter(user_id=self.user.id).first()
        if prof:
            self.assertFalse((prof.onboarding or {}).get("resume_reviewed"))

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self._patch({"resume_reviewed": True})
        self.assertIn(resp.status_code, (401, 403))

    def test_jsonapi_body_id_must_match_url_target(self):
        # Defense-in-depth: a JSON:API body whose data.id disagrees with
        # the URL target is refused. Catches confused clients and any
        # MITM rewrite that altered URL or body but not both.
        resp = self._patch({
            "data": {
                "type": "onboarding",
                "id": "999999",
                "attributes": {"subjective": {"wizard_enabled": False}},
            },
        })
        self.assertEqual(resp.status_code, 409, resp.content)
        # And nothing was applied to the actual user's profile.
        prof = Profile.objects.filter(user_id=self.user.id).first()
        if prof:
            self.assertNotIn("wizard_enabled", prof.onboarding or {})


class ProfileSplitOnboardingTests(TestCase):
    def test_split_partitions_known_keys_correctly(self):
        blob = Profile.default_onboarding()
        split = Profile.split_onboarding(blob)
        derived_keys = set(split["derived"].keys())
        subjective_keys = set(split["subjective"].keys())
        self.assertEqual(derived_keys, set(Profile.DERIVED_ONBOARDING_KEYS))
        self.assertEqual(subjective_keys, set(Profile.SUBJECTIVE_ONBOARDING_KEYS))
        # No overlap.
        self.assertTrue(derived_keys.isdisjoint(subjective_keys))


class ProfileBasicsCompleteTests(TestCase):
    """Single source of truth for the `profile_basics` derived flag.
    Used by both `derive_onboarding_from_state` (reconcile path) and the
    user serializer's read-time patch."""

    def test_returns_false_when_user_lacks_basics(self):
        user = User.objects.create_user(username="u1", password="p")
        prof = Profile.objects.create(user=user)
        self.assertFalse(prof.profile_basics_complete())

    def test_returns_true_when_user_has_first_last_email(self):
        user = User.objects.create_user(
            username="u1", password="p",
            first_name="Jane", last_name="Doe", email="jane@example.com",
        )
        prof = Profile.objects.create(user=user)
        self.assertTrue(prof.profile_basics_complete())

    def test_returns_false_when_one_field_missing(self):
        user = User.objects.create_user(
            username="u1", password="p",
            first_name="Jane", last_name="", email="jane@example.com",
        )
        prof = Profile.objects.create(user=user)
        self.assertFalse(prof.profile_basics_complete())


class DeriveOnboardingFromStateTests(TestCase):
    """The reconcile-derivation logic, lifted onto the Profile model so the
    view's reconcile_onboarding and any future caller share one rule."""

    def setUp(self):
        from job_hunting.models import Resume, JobPost, Score, CoverLetter, Company
        self.Resume = Resume
        self.JobPost = JobPost
        self.Score = Score
        self.CoverLetter = CoverLetter
        self.Company = Company
        self.user = User.objects.create_user(
            username="u1", password="p",
            first_name="Jane", last_name="Doe", email="jane@example.com",
        )
        self.prof = Profile.objects.create(user=self.user)

    def test_returns_only_derived_keys(self):
        derived = self.prof.derive_onboarding_from_state()
        self.assertEqual(set(derived.keys()), set(Profile.DERIVED_ONBOARDING_KEYS))

    def test_profile_basics_reflects_user_state(self):
        derived = self.prof.derive_onboarding_from_state()
        self.assertTrue(derived["profile_basics"])

    def test_resume_imported_flips_when_resume_exists(self):
        self.assertFalse(self.prof.derive_onboarding_from_state()["resume_imported"])
        self.Resume.objects.create(user=self.user, title="SRE")
        self.assertTrue(self.prof.derive_onboarding_from_state()["resume_imported"])

    def test_first_job_post_flips_when_jobpost_exists(self):
        company = self.Company.objects.create(name="Acme")
        self.JobPost.objects.create(
            title="Eng", company=company, created_by=self.user,
        )
        self.assertTrue(self.prof.derive_onboarding_from_state()["first_job_post"])

    def test_first_score_and_first_cover_letter(self):
        company = self.Company.objects.create(name="Acme")
        post = self.JobPost.objects.create(
            title="Eng", company=company, created_by=self.user,
        )
        self.Score.objects.create(user=self.user, job_post=post)
        self.CoverLetter.objects.create(user=self.user, content="Dear hiring...")
        derived = self.prof.derive_onboarding_from_state()
        self.assertTrue(derived["first_score"])
        self.assertTrue(derived["first_cover_letter"])


class MergeSubjectiveOnboardingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.prof = Profile.objects.create(user=self.user)

    def test_subjective_keys_apply_and_no_rejections(self):
        rejected = self.prof.merge_subjective_onboarding(
            {"resume_reviewed": True, "wizard_enabled": False}
        )
        self.assertEqual(rejected, set())
        self.assertTrue(self.prof.onboarding["resume_reviewed"])
        self.assertFalse(self.prof.onboarding["wizard_enabled"])

    def test_derived_keys_rejected_and_not_applied(self):
        rejected = self.prof.merge_subjective_onboarding({"resume_imported": True})
        self.assertEqual(rejected, {"resume_imported"})
        # Stored blob untouched.
        self.assertNotIn("resume_imported", self.prof.onboarding or {})

    def test_unknown_keys_rejected(self):
        rejected = self.prof.merge_subjective_onboarding({"bogus": True})
        self.assertEqual(rejected, {"bogus"})
