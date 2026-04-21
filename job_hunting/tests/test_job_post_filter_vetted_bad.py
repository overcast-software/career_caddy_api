from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost


User = get_user_model()


class TestJobPostListExcludeVettedBad(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="v", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def _triage(self, post, label):
        self.client.post(
            f"/api/v1/job-posts/{post.id}/triage/",
            data={"status": label},
            format="json",
        )

    def _ids(self, url):
        resp = self.client.get(url)
        return {int(row["id"]) for row in resp.json()["data"]}

    def test_excludes_posts_latest_status_vetted_bad(self):
        good = JobPost.objects.create(title="G", company=self.company, created_by=self.user)
        bad = JobPost.objects.create(title="B", company=self.company, created_by=self.user)
        untouched = JobPost.objects.create(title="U", company=self.company, created_by=self.user)
        self._triage(good, "Vetted Good")
        self._triage(bad, "Vetted Bad")

        all_ids = self._ids("/api/v1/job-posts/")
        self.assertIn(bad.id, all_ids)
        filtered = self._ids("/api/v1/job-posts/?filter[exclude_vetted_bad]=true")
        self.assertNotIn(bad.id, filtered)
        self.assertIn(good.id, filtered)
        self.assertIn(untouched.id, filtered)

    def test_reopens_when_newer_status_logged(self):
        post = JobPost.objects.create(title="R", company=self.company, created_by=self.user)
        self._triage(post, "Vetted Bad")
        self._triage(post, "Vetted Good")
        filtered = self._ids("/api/v1/job-posts/?filter[exclude_vetted_bad]=true")
        self.assertIn(post.id, filtered)
