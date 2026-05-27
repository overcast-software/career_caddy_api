from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import Company, JobPost

User = get_user_model()


class TestJobPostModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="jpuser", password="pass")
        self.company = Company.objects.create(name="Acme")

    def test_create_job_post(self):
        jp = JobPost.objects.create(title="Engineer", company=self.company, created_by=self.user)
        self.assertEqual(jp.title, "Engineer")
        self.assertEqual(jp.company, self.company)

    def test_nullable_fields(self):
        jp = JobPost.objects.create()
        self.assertIsNone(jp.title)
        self.assertIsNone(jp.description)
        self.assertIsNone(jp.company)

    def test_link_unique(self):
        JobPost.objects.create(link="https://example.com/job/1")
        with self.assertRaises(Exception):
            JobPost.objects.create(link="https://example.com/job/1")

    def test_apply_url_status_none_coerces_to_unknown(self):
        # Ember Data sends apply_url_status=null on createRecord; the
        # column is NOT NULL with no DB default, so save() must coerce.
        jp = JobPost(title="T", apply_url_status=None)
        jp.save()
        self.assertEqual(jp.apply_url_status, "unknown")

    def test_save_strips_trailing_junk_from_link_and_apply_url(self):
        # Regression for the 2026-05-27 hiring.cafe JP 2981 incident:
        # LLM URL extractor included the HTML closing `"`, the api
        # persisted it verbatim, the apply href 404'd in prod.
        jp = JobPost.objects.create(
            title="T",
            company=self.company,
            created_by=self.user,
            link='https://hiring.cafe/job/5fsbbgitg82ev1ar"',
            apply_url='https://ats.example.com/apply/42)',
        )
        jp.refresh_from_db()
        self.assertEqual(jp.link, "https://hiring.cafe/job/5fsbbgitg82ev1ar")
        self.assertEqual(jp.apply_url, "https://ats.example.com/apply/42")
        # canonical_link must also be clean — it's derived from link on
        # save, and is what the frontend dedup pipeline consults.
        self.assertEqual(
            jp.canonical_link, "https://hiring.cafe/job/5fsbbgitg82ev1ar"
        )


class TestJobPostAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="jpapi", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="TestCo")
        self.job_post = JobPost.objects.create(
            title="Dev", company=self.company, created_by=self.user
        )

    def test_list_job_posts(self):
        response = self.client.get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    def test_retrieve_job_post(self):
        response = self.client.get(f"/api/v1/job-posts/{self.job_post.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["attributes"]["title"], "Dev")

    def test_create_job_post(self):
        payload = {
            "data": {
                "type": "job-post",
                "attributes": {"title": "QA Engineer", "description": "Test everything"},
            }
        }
        response = self.client.post("/api/v1/job-posts/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])

    def test_update_job_post(self):
        payload = {
            "data": {
                "type": "job-post",
                "id": str(self.job_post.id),
                "attributes": {"title": "Senior Dev"},
            }
        }
        response = self.client.patch(
            f"/api/v1/job-posts/{self.job_post.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.job_post.refresh_from_db()
        self.assertEqual(self.job_post.title, "Senior Dev")

    def test_patch_ignores_readonly_computed_attributes(self):
        """Regression: the frontend echoes back attributes from a prior GET.
        top_score has no setter and must be popped. Triage state lives in
        meta.triage (never in attributes) so it can't even arrive here —
        this keeps the top_score guard honest."""
        payload = {
            "data": {
                "type": "job-post",
                "id": str(self.job_post.id),
                "attributes": {
                    "title": "Renamed",
                    "top_score": 42,
                },
            }
        }
        response = self.client.patch(
            f"/api/v1/job-posts/{self.job_post.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.job_post.refresh_from_db()
        self.assertEqual(self.job_post.title, "Renamed")

    def test_delete_job_post(self):
        jp = JobPost.objects.create(title="ToDelete", created_by=self.user)
        response = self.client.delete(f"/api/v1/job-posts/{jp.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(JobPost.objects.filter(pk=jp.id).exists())

    def test_retrieve_requires_auth(self):
        anon = APIClient()
        response = anon.get(f"/api/v1/job-posts/{self.job_post.id}/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_top_score_does_not_leak_across_users(self):
        """Regression: extension popup-open lookup (filter[link]) returns a
        JobPost any user can see (UX7). top_score MUST be the requesting
        user's score, never another user's. Previously the model property
        fell through `getattr(_top_score, None) or self.scores.first()`,
        leaking whoever-scored-first to whoever-loaded-second."""
        from job_hunting.models import Score
        other = User.objects.create_user(username="other", password="pass")
        shared = JobPost.objects.create(
            title="Shared", company=self.company, link="https://x.test/job/1",
            created_by=other,
        )
        # Other user has a score; self.user does not.
        Score.objects.create(job_post=shared, user=other, score=87)

        # filter[link] lookup (the extension's popup-open path)
        response = self.client.get(
            "/api/v1/job-posts/?filter[link]=https://x.test/job/1"
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]
        self.assertEqual(len(items), 1)
        # Current user has no score on this post → must be None, NOT 87.
        self.assertIsNone(items[0]["attributes"]["top_score"])

        # Once the requesting user scores it, top_score reflects THEIR score
        # — not the other user's higher 87.
        Score.objects.create(job_post=shared, user=self.user, score=55)
        response = self.client.get(
            "/api/v1/job-posts/?filter[link]=https://x.test/job/1"
        )
        items = response.json()["data"]
        self.assertEqual(items[0]["attributes"]["top_score"], 55)

        # Same guarantee on the retrieve endpoint (now reachable because the
        # user has a Score, which grants per-user access).
        retrieve = self.client.get(f"/api/v1/job-posts/{shared.id}/")
        self.assertEqual(retrieve.status_code, 200)
        self.assertEqual(retrieve.json()["data"]["attributes"]["top_score"], 55)


class TestJobPostEditApplyUrlCanonical(TestCase):
    """Phase 1 of Plans/PLAN ActivityPub prep + job-post adaptation:
    jp.edit surfaces apply_url editable + canonical_link read-only. Backend
    gate: created_by + staff may PATCH; everyone else 403."""

    def setUp(self):
        self.owner = User.objects.create_user(username="jp_owner", password="pass")
        self.staff = User.objects.create_user(
            username="jp_staff", password="pass", is_staff=True
        )
        self.stranger = User.objects.create_user(username="jp_stranger", password="pass")
        self.company = Company.objects.create(name="ApplyCo")
        self.jp = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            link="https://example.com/jobs/abc?utm_source=foo",
            created_by=self.owner,
        )

    def _patch(self, user, attributes):
        client = APIClient()
        client.force_authenticate(user=user)
        payload = {
            "data": {
                "type": "job-post",
                "id": str(self.jp.id),
                "attributes": attributes,
            }
        }
        return client.patch(
            f"/api/v1/job-posts/{self.jp.id}/", data=payload, format="json"
        )

    def test_owner_can_patch_apply_url(self):
        response = self._patch(self.owner, {"apply_url": "https://ats.example.com/123"})
        self.assertEqual(response.status_code, 200)
        self.jp.refresh_from_db()
        self.assertEqual(self.jp.apply_url, "https://ats.example.com/123")

    def test_staff_can_patch_apply_url_on_post_they_dont_own(self):
        response = self._patch(self.staff, {"apply_url": "https://ats.example.com/staff"})
        self.assertEqual(response.status_code, 200)
        self.jp.refresh_from_db()
        self.assertEqual(self.jp.apply_url, "https://ats.example.com/staff")

    def test_stranger_gets_403_on_patch(self):
        response = self._patch(self.stranger, {"apply_url": "https://nope.example.com/"})
        self.assertEqual(response.status_code, 403)
        self.jp.refresh_from_db()
        self.assertIsNone(self.jp.apply_url)

    def test_patch_link_re_derives_canonical_link(self):
        """The whole point of exposing canonical_link readonly: editing the
        URL must refresh the canonical form on save. Previously save() only
        set canonical_link when it was empty, so PATCH /link/ was a no-op
        on canonical_link — the duplicate-detection seam quietly broke."""
        # Initial canonicalize from setUp's link with utm_source.
        self.jp.refresh_from_db()
        original_canonical = self.jp.canonical_link
        self.assertIsNotNone(original_canonical)
        # New URL: same job, different tracking — canonical should equal the
        # tracking-stripped form.
        new_link = "https://example.com/jobs/abc?gh_src=tracker"
        response = self._patch(self.owner, {"link": new_link})
        self.assertEqual(response.status_code, 200)
        self.jp.refresh_from_db()
        # canonical_link is the tracking-stripped form, identical for both
        # variants of the same listing.
        self.assertNotIn("gh_src", self.jp.canonical_link or "")
        self.assertNotIn("utm_source", self.jp.canonical_link or "")

    def test_canonical_link_is_writable_in_serializer_but_save_overrides(self):
        """Serializer still lists canonical_link as writable so PATCH-with-
        echoed-attrs doesn't 400, but save() re-derives unconditionally so
        a client-sent value can't desync canonical_link from link."""
        response = self._patch(
            self.owner,
            {"link": "https://example.com/jobs/xyz", "canonical_link": "bogus"},
        )
        self.assertEqual(response.status_code, 200)
        self.jp.refresh_from_db()
        self.assertNotEqual(self.jp.canonical_link, "bogus")
        self.assertIn("example.com", self.jp.canonical_link or "")
