from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import JobPost


User = get_user_model()


class ResolveApplyEndpointTests(TestCase):
    """POST /api/v1/job-posts/{id}/resolve-apply/ — Phase 1 stub."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.other = User.objects.create_user(username="u2", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unknown_stays_unknown(self):
        jp = JobPost.objects.create(
            title="T", created_by=self.user, apply_url_status="unknown"
        )
        resp = self.client.post(f"/api/v1/job-posts/{jp.id}/resolve-apply/")
        self.assertEqual(resp.status_code, 202)
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url_status, "unknown")

    def test_stale_reset_to_unknown(self):
        jp = JobPost.objects.create(
            title="T", created_by=self.user, apply_url_status="stale"
        )
        self.client.post(f"/api/v1/job-posts/{jp.id}/resolve-apply/")
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url_status, "unknown")
        self.assertIsNone(jp.apply_url_resolved_at)

    def test_failed_reset_to_unknown(self):
        jp = JobPost.objects.create(
            title="T", created_by=self.user, apply_url_status="failed"
        )
        self.client.post(f"/api/v1/job-posts/{jp.id}/resolve-apply/")
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url_status, "unknown")

    def test_resolved_not_clobbered(self):
        jp = JobPost.objects.create(
            title="T",
            created_by=self.user,
            apply_url="https://ats.example/apply/1",
            apply_url_status="resolved",
        )
        self.client.post(f"/api/v1/job-posts/{jp.id}/resolve-apply/")
        jp.refresh_from_db()
        # Already-resolved posts keep their data; the resolver decides when
        # to re-run. Endpoint is idempotent for this state.
        self.assertEqual(jp.apply_url_status, "resolved")
        self.assertEqual(jp.apply_url, "https://ats.example/apply/1")

    def test_non_owner_denied(self):
        jp = JobPost.objects.create(title="T", created_by=self.other)
        resp = self.client.post(f"/api/v1/job-posts/{jp.id}/resolve-apply/")
        self.assertEqual(resp.status_code, 403)

    def test_staff_allowed_across_users(self):
        staff = User.objects.create_user(username="admin", password="p", is_staff=True)
        self.client.force_authenticate(user=staff)
        jp = JobPost.objects.create(
            title="T", created_by=self.other, apply_url_status="failed"
        )
        resp = self.client.post(f"/api/v1/job-posts/{jp.id}/resolve-apply/")
        self.assertEqual(resp.status_code, 202)
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url_status, "unknown")

    def test_default_fields_on_fresh_post(self):
        jp = JobPost.objects.create(title="Fresh", created_by=self.user)
        self.assertEqual(jp.apply_url_status, "unknown")
        self.assertIsNone(jp.apply_url)
        self.assertIsNone(jp.apply_url_resolved_at)
