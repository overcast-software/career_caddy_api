"""
Regression tests for the score create and list bugs:

1. ScoreViewSet.create(): staff caller with no explicit user relationship
   should create the score under the job post's creator, not the daemon account.

2. ScoreViewSet.list(): filter[job_post_id] scopes results so the frontend
   scores route (store.query with filter[job_post_id]) gets only the scores
   for the visible job post.

3. JobPostSerializer.get_related("scores"): linked_relationships must only
   emit score IDs belonging to the requesting user, not all users' scores.
"""
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Score

User = get_user_model()
SCORES_URL = "/api/v1/scores/"
JP_URL = "/api/v1/job-posts/"


class TestScoreListJobPostFilter(TestCase):
    """filter[job_post_id] narrows the per-user score list."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="scorer", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.jp_a = JobPost.objects.create(
            title="Engineer A", company=self.company, created_by=self.user,
            description="x " * 80,
        )
        self.jp_b = JobPost.objects.create(
            title="Engineer B", company=self.company, created_by=self.user,
            description="x " * 80,
        )
        self.score_a = Score.objects.create(job_post=self.jp_a, user=self.user, score=75)
        self.score_b = Score.objects.create(job_post=self.jp_b, user=self.user, score=60)

    def _ids(self, resp):
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return {int(r["id"]) for r in resp.json()["data"]}

    def test_no_filter_returns_all_user_scores(self):
        ids = self._ids(self.client.get(SCORES_URL))
        self.assertIn(self.score_a.id, ids)
        self.assertIn(self.score_b.id, ids)

    def test_filter_by_job_post_id_returns_only_that_posts_scores(self):
        ids = self._ids(self.client.get(SCORES_URL + f"?filter[job_post_id]={self.jp_a.id}"))
        self.assertIn(self.score_a.id, ids)
        self.assertNotIn(self.score_b.id, ids)

    def test_filter_by_other_post_id_excludes_unrelated_scores(self):
        ids = self._ids(self.client.get(SCORES_URL + f"?filter[job_post_id]={self.jp_b.id}"))
        self.assertNotIn(self.score_a.id, ids)
        self.assertIn(self.score_b.id, ids)

    def test_filter_by_nonexistent_post_id_returns_empty(self):
        ids = self._ids(self.client.get(SCORES_URL + "?filter[job_post_id]=99999"))
        self.assertEqual(len(ids), 0)

    def test_filter_by_invalid_id_is_ignored(self):
        # Bad values must not crash — just return unfiltered list
        resp = self.client.get(SCORES_URL + "?filter[job_post_id]=notanint")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class TestScoreCreateStaffInference(TestCase):
    """When a staff service-account caller omits the user relationship, the
    score should land under the job post's creator, not the daemon account."""

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.daemon = User.objects.create_user(
            username="daemon", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")
        self.jp = JobPost.objects.create(
            title="Engineer", company=self.company, created_by=self.owner,
            description="a " * 100,
        )

    def _post_score(self, as_user):
        client = APIClient()
        client.force_authenticate(user=as_user)
        payload = {
            "data": {
                "type": "score",
                "attributes": {},
                "relationships": {
                    "job-post": {"data": {"type": "job-post", "id": str(self.jp.id)}}
                },
            }
        }
        with patch("job_hunting.api.views.scores.threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: None
            resp = client.post(
                SCORES_URL,
                data=payload,
                content_type="application/vnd.api+json",
            )
        return resp

    def test_staff_caller_creates_score_under_jp_creator(self):
        resp = self._post_score(as_user=self.daemon)
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        score = Score.objects.filter(job_post=self.jp).first()
        self.assertIsNotNone(score)
        self.assertEqual(
            score.user_id,
            self.owner.id,
            "Staff daemon must create score under job post creator, not daemon account",
        )

    def test_regular_user_caller_creates_score_under_themselves(self):
        resp = self._post_score(as_user=self.owner)
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        score = Score.objects.filter(job_post=self.jp).first()
        self.assertIsNotNone(score)
        self.assertEqual(score.user_id, self.owner.id)


class TestScoreMultiTenancy(TestCase):
    """Scores must never cross user boundaries — including shared job posts
    that are visible to multiple users via discovery or applications."""

    def setUp(self):
        self.creator = User.objects.create_user(username="creator", password="pw")
        self.visitor = User.objects.create_user(username="visitor", password="pw")
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")
        self.jp = JobPost.objects.create(
            title="Engineer", company=self.company, created_by=self.creator,
            description="a " * 100,
        )

    def _post_score(self, as_user, job_post_id=None):
        client = APIClient()
        client.force_authenticate(user=as_user)
        jp_id = job_post_id or self.jp.id
        payload = {
            "data": {
                "type": "score",
                "attributes": {},
                "relationships": {
                    "job-post": {"data": {"type": "job-post", "id": str(jp_id)}}
                },
            }
        }
        with patch("job_hunting.api.views.scores.threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: None
            return client.post(
                SCORES_URL,
                data=payload,
                content_type="application/vnd.api+json",
            )

    def test_non_staff_visitor_score_belongs_to_visitor_not_creator(self):
        """User B creates a score for a post they have visibility to (but
        didn't create). Score must be attributed to User B, not to the
        job post's creator."""
        resp = self._post_score(as_user=self.visitor)
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        score = Score.objects.filter(job_post=self.jp).first()
        self.assertIsNotNone(score)
        self.assertEqual(
            score.user_id,
            self.visitor.id,
            "Non-staff user's score must be their own, not re-routed to jp creator",
        )

    def test_staff_daemon_score_goes_to_jp_creator(self):
        resp = self._post_score(as_user=self.staff)
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        score = Score.objects.filter(job_post=self.jp).first()
        self.assertEqual(score.user_id, self.creator.id)

    def test_visitor_list_excludes_creator_scores(self):
        Score.objects.create(job_post=self.jp, user=self.creator, score=80)
        client = APIClient()
        client.force_authenticate(user=self.visitor)
        resp = client.get(SCORES_URL + f"?filter[job_post_id]={self.jp.id}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        creator_score_ids = set(
            Score.objects.filter(job_post=self.jp, user=self.creator).values_list("id", flat=True)
        )
        self.assertTrue(ids.isdisjoint(creator_score_ids), "Scores must never cross users")


class TestJobPostLinkedScoresUserScoped(TestCase):
    """linked_relationships for scores on a job post must only include
    IDs belonging to the requesting user, not other users' scores."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pw")
        self.other = User.objects.create_user(username="u2", password="pw")
        self.company = Company.objects.create(name="Acme")
        self.jp = JobPost.objects.create(
            title="Engineer", company=self.company, created_by=self.user,
            description="a " * 100,
        )
        self.my_score = Score.objects.create(job_post=self.jp, user=self.user, score=70)
        self.other_score = Score.objects.create(job_post=self.jp, user=self.other, score=55)

    def _score_ids_in_jp_response(self, as_user):
        client = APIClient()
        client.force_authenticate(user=as_user)
        resp = client.get(JP_URL + f"{self.jp.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rel = resp.json()["data"]["relationships"].get("scores", {})
        data = rel.get("data") or []
        return {int(r["id"]) for r in data}

    def test_user_only_sees_own_score_ids_in_linkage(self):
        ids = self._score_ids_in_jp_response(as_user=self.user)
        self.assertIn(self.my_score.id, ids)
        self.assertNotIn(
            self.other_score.id,
            ids,
            "linked_relationships must not expose other users' score IDs",
        )

    def test_other_user_only_sees_their_score_ids(self):
        ids = self._score_ids_in_jp_response(as_user=self.other)
        self.assertIn(self.other_score.id, ids)
        self.assertNotIn(self.my_score.id, ids)
