"""Tests for GET /api/v1/job-posts/:id/duplicate-candidates/."""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape


User = get_user_model()


class TestDuplicateCandidates(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="dupe", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.user)
        self.snbl = Company.objects.create(name="SNBL USA")

    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/duplicate-candidates/"

    def test_returns_404_for_unknown_post(self):
        resp = self.client.get("/api/v1/job-posts/999999/duplicate-candidates/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_empty_when_no_other_posts(self):
        jp = JobPost.objects.create(
            title="Lone Ranger", company=self.snbl, created_by=self.user
        )
        resp = self.client.get(self._url(jp))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {"data": []})

    def test_self_excluded(self):
        # Even when only one row exists for a fingerprint, the row itself
        # must never appear in its own candidate list.
        jp = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        resp = self.client.get(self._url(jp))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"], [])

    def test_canonical_link_match_high_confidence(self):
        # Use two distinct links that canonicalize to the same value
        # (tracking-param stripping). JobPost.link has a unique
        # constraint, so we can't store the literal same string twice.
        a = JobPost.objects.create(
            title="Engineer A",
            company=self.snbl,
            created_by=self.user,
            link="https://example.com/jobs/eng-42?utm_source=foo",
        )
        b = JobPost.objects.create(
            title="Engineer B",
            company=self.snbl,
            created_by=self.user,
            link="https://example.com/jobs/eng-42?utm_source=bar",
        )
        # Sanity: canonical_link must match for the candidate scan to fire.
        self.assertEqual(a.canonical_link, b.canonical_link)
        resp = self.client.get(self._url(a))
        body = resp.json()["data"]
        self.assertEqual(len(body), 1)
        cand = body[0]
        self.assertEqual(cand["id"], str(b.id))
        self.assertEqual(cand["attributes"]["confidence"], "high")
        self.assertIn("canonical_link", cand["attributes"]["match_signals"])
        # fingerprint also matches (same company + same normalized title? No,
        # titles differ. So only canonical_link should fire.)
        self.assertNotIn("fingerprint", cand["attributes"]["match_signals"])

    def test_fingerprint_match_high_confidence(self):
        # Same company + same normalized title → same content_fingerprint.
        a = JobPost.objects.create(
            title="Senior Engineer",
            company=self.snbl,
            created_by=self.user,
            link="https://example.com/a",
        )
        JobPost.objects.create(
            title="Senior Engineer",
            company=self.snbl,
            created_by=self.user,
            link="https://other.com/b",
        )
        resp = self.client.get(self._url(a))
        body = resp.json()["data"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["attributes"]["confidence"], "high")
        self.assertIn("fingerprint", body[0]["attributes"]["match_signals"])

    def test_title_suffix_drift_medium_confidence(self):
        # The jp 999 vs jp 1428 case: same company, one title is the
        # other plus a trailing suffix. Different fingerprints.
        a = JobPost.objects.create(
            title="START UP BUS DEV & PROG MGR (Bi-Lingual, Japanese/English)",
            company=self.snbl,
            created_by=self.user,
        )
        b = JobPost.objects.create(
            title="START UP BUS DEV & PROG MGR (Bi-Lingual, Japanese/English) 75-100% FTE",
            company=self.snbl,
            created_by=self.user,
        )
        resp = self.client.get(self._url(a))
        body = resp.json()["data"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["id"], str(b.id))
        self.assertEqual(body[0]["attributes"]["confidence"], "medium")
        self.assertIn("title_similarity", body[0]["attributes"]["match_signals"])

    def test_already_marked_duplicate_excluded(self):
        # Settled relationships shouldn't re-surface.
        canonical = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        dupe = JobPost.objects.create(
            title="Engineer",
            company=self.snbl,
            created_by=self.user,
            duplicate_of=canonical,
        )
        # When viewing the duplicate, its parent is settled — don't suggest.
        resp = self.client.get(self._url(dupe))
        self.assertEqual(resp.json()["data"], [])
        # When viewing the canonical, its child is settled too.
        resp = self.client.get(self._url(canonical))
        self.assertEqual(resp.json()["data"], [])

    def test_non_staff_only_sees_own_visible_candidates(self):
        # Reset to a non-staff user.
        self.user = User.objects.create_user(username="reg", password="pw")
        self.client.force_authenticate(user=self.user)

        owned = JobPost.objects.create(
            title="Engineer",
            company=self.snbl,
            created_by=self.user,
        )
        other_user = User.objects.create_user(username="other", password="pw")
        # Same fingerprint, but owned by a different user with no shared
        # signal — must not surface.
        JobPost.objects.create(
            title="Engineer",
            company=self.snbl,
            created_by=other_user,
        )
        resp = self.client.get(self._url(owned))
        self.assertEqual(resp.json()["data"], [])

    def test_payload_shape(self):
        a = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        b = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        resp = self.client.get(self._url(a))
        cand = resp.json()["data"][0]
        self.assertEqual(cand["type"], "job-post-duplicate-candidate")
        self.assertEqual(cand["id"], str(b.id))
        attrs = cand["attributes"]
        self.assertEqual(attrs["title"], "Engineer")
        self.assertEqual(attrs["company_name"], "SNBL USA")
        self.assertEqual(attrs["frontend_url"], f"/job-posts/{b.id}")
        self.assertIn(attrs["confidence"], ("high", "medium", "low"))
        self.assertIsInstance(attrs["match_signals"], list)

    def test_jp_payload_always_emits_duplicate_candidates_links_related(self):
        # Without this block, jp.show's `await jp.hasMany('duplicateCandidates')
        # .reload()` silently no-ops in Ember Data — no link to follow, so
        # findHasMany is never invoked. Banner stays empty even when the
        # /duplicate-candidates/ endpoint would have returned candidates.
        jp = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        resp = self.client.get(f"/api/v1/job-posts/{jp.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rels = resp.json()["data"]["relationships"]
        self.assertIn("duplicate-candidates", rels)
        self.assertEqual(
            rels["duplicate-candidates"]["links"]["related"],
            f"/api/v1/job-posts/{jp.id}/duplicate-candidates",
        )

    def test_include_duplicate_candidates_sideloads_resources(self):
        # The cleaner one-roundtrip path: jp.show route uses
        # `?include=duplicate-candidates`. Framework emits both data linkage
        # and top-level `included[]` in the same payload so Ember Data can
        # populate the hasMany without a second request.
        a = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        b = JobPost.objects.create(
            title="Engineer", company=self.snbl, created_by=self.user
        )
        resp = self.client.get(
            f"/api/v1/job-posts/{a.id}/?include=duplicate-candidates"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        rels = body["data"]["relationships"]["duplicate-candidates"]
        self.assertEqual(
            rels["data"], [{"type": "job-post-duplicate-candidate", "id": str(b.id)}]
        )
        included = body.get("included", [])
        cand_resources = [
            r for r in included if r["type"] == "job-post-duplicate-candidate"
        ]
        self.assertEqual(len(cand_resources), 1)
        self.assertEqual(cand_resources[0]["id"], str(b.id))
        self.assertEqual(cand_resources[0]["attributes"]["title"], "Engineer")


class TestDuplicateCandidatesExtensionHints(TestCase):
    """Bidirectional cross-platform dedup: JobPost.apply_url pointing at
    JP-A surfaces the source JP-B as a candidate on JP-A's panel
    (apply_hint signal); a Scrape.referrer_url pointing at JP-A surfaces
    the parent JP-B as a referrer_hint candidate."""

    LINKEDIN = "https://www.linkedin.com/jobs/view/4400000001/"
    ATS = "https://ats.rippling.com/rippling/jobs/abc-001"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="hintdup", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Rippling")

    def _candidates(self, jp):
        resp = self.client.get(f"/api/v1/job-posts/{jp.id}/duplicate-candidates/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.json()["data"]

    def test_apply_url_surfaces_linkedin_jp_on_ats_panel(self):
        """Setup: user submits the LinkedIn JP with apply_url=ATS.
        JobPost.apply_url stores the cross-platform link directly. The
        ATS JP exists separately. Opening the ATS JP must surface the
        LinkedIn JP via the apply_hint signal."""
        ats_jp = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            link=self.ATS,
            created_by=self.user,
        )
        linkedin_jp = JobPost.objects.create(
            title="Engineer (LinkedIn)",
            company=self.company,
            link=self.LINKEDIN,
            apply_url=self.ATS,
            created_by=self.user,
        )
        candidates = self._candidates(ats_jp)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], str(linkedin_jp.id))
        self.assertIn(
            "apply_hint", candidates[0]["attributes"]["match_signals"]
        )
        self.assertEqual(candidates[0]["attributes"]["confidence"], "high")

    def test_apply_url_surfaces_ats_jp_on_linkedin_panel(self):
        """Reverse leg of the same relationship: opening the LinkedIn JP
        (whose apply_url points at the ATS JP) must also surface the ATS
        JP as a candidate. JobPost-direct join works both ways."""
        ats_jp = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            link=self.ATS,
            created_by=self.user,
        )
        linkedin_jp = JobPost.objects.create(
            title="Engineer (LinkedIn)",
            company=self.company,
            link=self.LINKEDIN,
            apply_url=self.ATS,
            created_by=self.user,
        )
        candidates = self._candidates(linkedin_jp)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], str(ats_jp.id))
        self.assertIn(
            "apply_hint", candidates[0]["attributes"]["match_signals"]
        )

    def test_referrer_hint_surfaces_ats_jp_on_linkedin_panel(self):
        """Symmetric case: user submits the ATS JP after clicking through
        LinkedIn, so Scrape.referrer_url=LINKEDIN. Opening the LinkedIn
        JP must surface the ATS JP via the referrer_hint signal."""
        linkedin_jp = JobPost.objects.create(
            title="Engineer (LinkedIn)",
            company=self.company,
            link=self.LINKEDIN,
            created_by=self.user,
        )
        ats_jp = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            link=self.ATS,
            created_by=self.user,
        )
        Scrape.objects.create(
            url=self.ATS,
            job_post=ats_jp,
            referrer_url=self.LINKEDIN,
            created_by=self.user,
        )
        candidates = self._candidates(linkedin_jp)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], str(ats_jp.id))
        self.assertIn(
            "referrer_hint", candidates[0]["attributes"]["match_signals"]
        )

    def test_no_hint_no_candidate(self):
        """A Scrape without hints contributes no cross-platform signal.

        Uses titles that don't share a prefix/suffix to avoid the
        existing title_similarity heuristic firing — this test is about
        the hint signals specifically.
        """
        ats_jp = JobPost.objects.create(
            title="Backend Engineer",
            company=self.company,
            link=self.ATS,
            created_by=self.user,
        )
        linkedin_jp = JobPost.objects.create(
            title="Frontend Architect",
            company=self.company,
            link=self.LINKEDIN,
            created_by=self.user,
        )
        Scrape.objects.create(
            url=self.LINKEDIN, job_post=linkedin_jp, created_by=self.user
        )
        self.assertEqual(self._candidates(ats_jp), [])
