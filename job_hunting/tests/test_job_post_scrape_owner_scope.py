from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import JobPost, Scrape


User = get_user_model()


class JobPostVisibilityViaScrapeTests(TestCase):
    """Pasting a link that dedups to someone else's JobPost should still
    surface the post in the pasting user's /job-posts list and retrieve.
    Regression: before this fix, the 3-way access check (created_by /
    applications / scores) excluded dedup-linked posts, forcing users to
    navigate via the scrape to reach their own content."""

    def setUp(self):
        self.automator = User.objects.create_user(username="cc_auto", password="p")
        self.me = User.objects.create_user(username="me", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.me)

        # A JobPost created by the email-automation service user.
        self.post = JobPost.objects.create(
            title="Senior Engineer @ Acme",
            created_by=self.automator,
            link="https://boards.greenhouse.io/acme/jobs/42",
        )

    def test_dedup_linked_post_appears_in_list(self):
        # Before paste: post isn't mine — not in my list.
        resp = self.client.get("/api/v1/job-posts/")
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertNotIn(str(self.post.id), ids)

        # I paste/scrape the same link — my scrape is linked to the post.
        Scrape.objects.create(
            url=self.post.link,
            created_by=self.me,
            job_post=self.post,
            status="completed",
        )

        # Now it shows up in my list.
        resp = self.client.get("/api/v1/job-posts/")
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertIn(str(self.post.id), ids)

    def test_dedup_linked_post_is_retrievable(self):
        Scrape.objects.create(
            url=self.post.link,
            created_by=self.me,
            job_post=self.post,
            status="completed",
        )
        resp = self.client.get(f"/api/v1/job-posts/{self.post.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_unrelated_post_still_hidden(self):
        other = JobPost.objects.create(
            title="Other", created_by=self.automator, link="https://other/jobs/1"
        )
        resp = self.client.get("/api/v1/job-posts/")
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertNotIn(str(other.id), ids)
        resp = self.client.get(f"/api/v1/job-posts/{other.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_other_users_scrape_does_not_grant_me_access(self):
        third = User.objects.create_user(username="third", password="p")
        Scrape.objects.create(
            url=self.post.link,
            created_by=third,
            job_post=self.post,
            status="completed",
        )
        # Only `third` has a scrape — I should still not see it.
        resp = self.client.get("/api/v1/job-posts/")
        ids = [row["id"] for row in resp.json()["data"]]
        self.assertNotIn(str(self.post.id), ids)
