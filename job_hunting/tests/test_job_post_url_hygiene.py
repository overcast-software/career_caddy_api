"""Integration: POST /api/v1/job-posts/ URL hygiene (Phase 0 lift).

Mirrors the URL-policy gate POST /api/v1/scrapes/ already runs so the
direct-create path (cc_auto inbox triage, MCP create_job_post tool,
manual create form) cannot leave junk schemes, our own domain, or
private hosts on JobPost.link.

Tracker-redirect resolution (LinkedIn /comm/ etc.) is NOT lifted in
this slice — see todo.org "Ingest abuse defense — Phase 2" for the
deterministic path-rewrite approach that actually works for hosts
that gate behind auth.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import JobPost


User = get_user_model()


def _payload(link, **attrs):
    body_attrs = {"link": link, **attrs}
    return {"data": {"type": "job-post", "attributes": body_attrs}}


class TestJobPostUrlPolicy(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u-policy", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, link, **attrs):
        return self.client.post(
            "/api/v1/job-posts/", _payload(link, **attrs), format="json",
        )

    def test_rejects_non_http_scheme(self):
        r = self._post("javascript:alert(1)")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["errors"][0]["code"], "blocked_scheme")
        self.assertEqual(JobPost.objects.count(), 0)

    def test_rejects_self_host(self):
        r = self._post("https://careercaddy.online/job-posts/1")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["errors"][0]["code"], "blocked_self")
        self.assertEqual(JobPost.objects.count(), 0)

    def test_rejects_rfc1918(self):
        r = self._post("http://10.0.0.5/job")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["errors"][0]["code"], "blocked_private")
        self.assertEqual(JobPost.objects.count(), 0)

    def test_rejects_dot_local(self):
        r = self._post("http://api.local/job")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["errors"][0]["code"], "blocked_private")
        self.assertEqual(JobPost.objects.count(), 0)

    def test_clean_url_creates(self):
        r = self._post("https://example.com/careers/123", title="Engineer")
        self.assertIn(r.status_code, (200, 201))
        self.assertEqual(JobPost.objects.count(), 1)
        jp = JobPost.objects.get()
        self.assertEqual(jp.link, "https://example.com/careers/123")

    def test_no_link_skips_url_policy(self):
        # Manual creates without a link (just a pasted description) must
        # still work — policy only fires when a link is provided.
        r = self.client.post(
            "/api/v1/job-posts/",
            {"data": {"type": "job-post", "attributes": {"title": "Standalone"}}},
            format="json",
        )
        self.assertIn(r.status_code, (200, 201))
        self.assertEqual(JobPost.objects.count(), 1)
