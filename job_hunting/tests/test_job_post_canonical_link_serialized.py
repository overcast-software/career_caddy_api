"""JobPostSerializer surfaces `canonical_link` so writers can debug dedupe.

Background: cc_auto's email pipeline used to strip tracking params locally
before POSTing, and api re-canonicalized server-side via JobPost.save(). The
two strip-lists drifted — same URL canonicalized to different forms in each
side. The fix is to keep cc_auto's `link` raw and let api's save() compute
`canonical_link` once. This test guards the contract: the response payload
must include `canonical_link` so cc_auto can read it back for logging /
dedupe-prediction.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost

User = get_user_model()


class TestJobPostCanonicalLinkSerialized(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="cl", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def test_canonical_link_in_attributes(self):
        jp = JobPost.objects.create(
            title="Eng",
            company=self.company,
            link="https://example.com/job/42?utm_source=linkedin",
            created_by=self.user,
        )
        resp = self.client.get(f"/api/v1/job-posts/{jp.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("canonical_link", attrs)
        self.assertIn("link", attrs)
        # link is preserved as-sent; canonical_link is the dedupe key
        self.assertIn("utm_source", attrs["link"])
        self.assertNotIn("utm_source", attrs["canonical_link"])

    def test_canonical_link_null_when_link_null(self):
        jp = JobPost.objects.create(
            title="Direct-solicitation post",
            company=self.company,
            link=None,
            source="email_direct",
            created_by=self.user,
        )
        resp = self.client.get(f"/api/v1/job-posts/{jp.id}/")
        attrs = resp.json()["data"]["attributes"]
        self.assertIsNone(attrs["link"])
        self.assertIsNone(attrs["canonical_link"])
