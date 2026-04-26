from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import JobPost, Scrape


User = get_user_model()


class ScrapeApplyUrlEndpointTests(TestCase):
    """PATCH /api/v1/scrapes/{id}/apply-url/ — resolver write path."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.other = User.objects.create_user(username="u2", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _make(self, **kw):
        jp = JobPost.objects.create(title="T", created_by=self.user, **kw)
        sc = Scrape.objects.create(
            url="https://example.com/job/1",
            created_by=self.user,
            job_post=jp,
        )
        return jp, sc

    def _patch(self, scrape_id, status="resolved", url=None):
        body = {"data": {"attributes": {"apply_url_status": status}}}
        if url is not None:
            body["data"]["attributes"]["apply_url"] = url
        return self.client.patch(
            f"/api/v1/scrapes/{scrape_id}/apply-url/",
            body,
            format="json",
        )

    def test_resolved_writes_to_scrape_and_job_post(self):
        jp, sc = self._make()
        resp = self._patch(sc.id, "resolved", "https://ats.example/apply/1")
        self.assertEqual(resp.status_code, 200)
        sc.refresh_from_db()
        jp.refresh_from_db()
        self.assertEqual(sc.apply_url, "https://ats.example/apply/1")
        self.assertEqual(sc.apply_url_status, "resolved")
        self.assertEqual(jp.apply_url, "https://ats.example/apply/1")
        self.assertEqual(jp.apply_url_status, "resolved")
        self.assertIsNotNone(jp.apply_url_resolved_at)

    def test_internal_no_url_required(self):
        jp, sc = self._make()
        resp = self._patch(sc.id, "internal", None)
        self.assertEqual(resp.status_code, 200)
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url_status, "internal")
        self.assertIsNone(jp.apply_url)
        self.assertIsNotNone(jp.apply_url_resolved_at)

    def test_failed_does_not_stamp_resolved_at(self):
        jp, sc = self._make()
        resp = self._patch(sc.id, "failed", None)
        self.assertEqual(resp.status_code, 200)
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url_status, "failed")
        self.assertIsNone(jp.apply_url_resolved_at)

    def test_invalid_status_rejected(self):
        _, sc = self._make()
        resp = self._patch(sc.id, "bogus")
        self.assertEqual(resp.status_code, 400)

    def test_scrape_without_job_post_still_writes(self):
        sc = Scrape.objects.create(
            url="https://example.com/job/2", created_by=self.user
        )
        resp = self._patch(sc.id, "resolved", "https://ats.example/apply/2")
        self.assertEqual(resp.status_code, 200)
        sc.refresh_from_db()
        self.assertEqual(sc.apply_url_status, "resolved")

    def test_non_owner_denied(self):
        sc = Scrape.objects.create(
            url="https://example.com/job/3", created_by=self.other
        )
        resp = self._patch(sc.id, "resolved", "https://ats.example/3")
        self.assertEqual(resp.status_code, 403)

    def test_staff_allowed_across_users(self):
        staff = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )
        self.client.force_authenticate(user=staff)
        sc = Scrape.objects.create(
            url="https://example.com/job/4", created_by=self.other
        )
        resp = self._patch(sc.id, "resolved", "https://ats.example/4")
        self.assertEqual(resp.status_code, 200)

    def test_url_too_long_rejected(self):
        _, sc = self._make()
        resp = self._patch(sc.id, "resolved", "https://x/" + ("a" * 3000))
        self.assertEqual(resp.status_code, 400)

    def test_post_method_also_works(self):
        _, sc = self._make()
        resp = self.client.post(
            f"/api/v1/scrapes/{sc.id}/apply-url/",
            {"data": {"attributes": {
                "apply_url_status": "resolved",
                "apply_url": "https://ats.example/p",
            }}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_apply_candidates_round_trip(self):
        """Phase 3: when the resolver ends in unknown/failed it captures
        candidate Apply elements for later aggregation. Persist verbatim."""
        _, sc = self._make()
        candidates = [
            {
                "selector": "a.btn.apply",
                "href": "https://ats.example/apply",
                "text": "Apply Now",
                "tag": "a",
                "score": 0.9,
                "reason": "href contains 'apply' AND text 'apply'",
            },
            {
                "selector": "button[data-test='easy-apply']",
                "href": None,
                "text": "Easy Apply",
                "tag": "button",
                "score": 0.7,
                "reason": "data-test attr contains 'easy-apply'",
            },
        ]
        resp = self.client.patch(
            f"/api/v1/scrapes/{sc.id}/apply-url/",
            {"data": {"attributes": {
                "apply_url_status": "failed",
                "apply_candidates": candidates,
            }}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        sc.refresh_from_db()
        self.assertEqual(sc.apply_candidates, candidates)
        self.assertEqual(sc.apply_url_status, "failed")

    def test_apply_candidates_capped_at_50(self):
        _, sc = self._make()
        many = [{"selector": f"a.c{i}", "href": "https://x/y", "text": "Apply",
                 "tag": "a", "score": 0.1, "reason": "spam"}
                for i in range(120)]
        resp = self.client.patch(
            f"/api/v1/scrapes/{sc.id}/apply-url/",
            {"data": {"attributes": {
                "apply_url_status": "failed",
                "apply_candidates": many,
            }}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        sc.refresh_from_db()
        self.assertEqual(len(sc.apply_candidates), 50)

    def test_apply_candidates_must_be_list(self):
        _, sc = self._make()
        resp = self.client.patch(
            f"/api/v1/scrapes/{sc.id}/apply-url/",
            {"data": {"attributes": {
                "apply_url_status": "failed",
                "apply_candidates": {"oops": "this is a dict"},
            }}},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_apply_candidates_omitted_does_not_clobber(self):
        """When the field is omitted on a subsequent PATCH, prior
        candidates stay intact (capture is preserved, not overwritten by
        a later resolved-status PATCH that has no candidates to report)."""
        _, sc = self._make()
        sc.apply_candidates = [{"selector": "a.x", "href": "h", "text": "t",
                                "tag": "a", "score": 0.5, "reason": "r"}]
        sc.save(update_fields=["apply_candidates"])
        resp = self.client.patch(
            f"/api/v1/scrapes/{sc.id}/apply-url/",
            {"data": {"attributes": {"apply_url_status": "resolved",
                                      "apply_url": "https://x/y"}}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        sc.refresh_from_db()
        self.assertEqual(len(sc.apply_candidates), 1)
        self.assertEqual(sc.apply_url_status, "resolved")


class ScrapeProfileApplyConfigTests(TestCase):
    """ScrapeProfile.apply_resolver_config round-trips through the serializer."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_round_trip(self):
        from job_hunting.models import ScrapeProfile
        cfg = {
            "internal_apply_markers": [".jobs-apply-button--easy-apply"],
            "apply_link_selectors": ["a[data-tracking='apply']"],
            "apply_button_selectors": ["button.apply"],
        }
        profile = ScrapeProfile.objects.create(
            hostname="example.com", apply_resolver_config=cfg
        )
        resp = self.client.get(f"/api/v1/scrape-profiles/{profile.id}/")
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs.get("apply_resolver_config"), cfg)
