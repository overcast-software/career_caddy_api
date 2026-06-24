"""CC-61 — ``filter[publishable]=true`` curation-queue list filter on
JobPostViewSet.

Surfaces the owner's vetted-but-unpublished candidates:
``created_by == me AND NOT public (AS2_PUBLIC not in audience) AND
(has Score OR JobApplication OR direct Question)``. Excludes
already-public posts and bare stubs the owner never engaged with.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.models import JobApplication, JobPost, Question, Score
from job_hunting.models.job_post import AS2_PUBLIC

User = get_user_model()


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestPublishableFilter(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="cur_owner", password="pass")
        self.other = User.objects.create_user(username="cur_other", password="pass")
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _post(self, *, audience=None, created_by=None):
        return JobPost.objects.create(
            created_by=created_by or self.owner,
            title="Role",
            description="d",
            audience=[] if audience is None else audience,
        )

    def _publishable_ids(self):
        resp = self.client.get("/api/v1/job-posts/?filter[publishable]=true")
        self.assertEqual(resp.status_code, 200)
        return {int(r["id"]) for r in resp.data["data"]}

    def test_includes_own_vetted_via_score(self):
        post = self._post()
        Score.objects.create(job_post=post, user=self.owner, score=80)
        self.assertIn(post.id, self._publishable_ids())

    def test_includes_own_vetted_via_application(self):
        post = self._post()
        JobApplication.objects.create(job_post=post, user=self.owner)
        self.assertIn(post.id, self._publishable_ids())

    def test_includes_own_vetted_via_question(self):
        post = self._post()
        Question.objects.create(job_post=post, created_by=self.owner, content="q")
        self.assertIn(post.id, self._publishable_ids())

    def test_excludes_already_public(self):
        post = self._post(audience=[AS2_PUBLIC])
        Score.objects.create(job_post=post, user=self.owner, score=50)
        self.assertNotIn(post.id, self._publishable_ids())

    def test_excludes_bare_stub(self):
        post = self._post()  # no Score / JobApplication / Question
        self.assertNotIn(post.id, self._publishable_ids())

    def test_excludes_other_users_vetted_post(self):
        post = self._post(created_by=self.other)
        Score.objects.create(job_post=post, user=self.other, score=90)
        self.assertNotIn(post.id, self._publishable_ids())
