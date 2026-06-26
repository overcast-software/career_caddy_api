"""CC-91 regression: JSON:API list serialization must not be a per-row N+1.

Serializing a list of JobApplications (or Scores) used to issue ~3-6 queries
*per row* — BaseSerializer.to_resource lazy-loaded every to-one FK object and
called get_related() per row for each linked_relationship, and the viewsets
fetched the page with no select_related/prefetch_related. On prod (NanoID DB)
this turned `job-applications?filter[company]=` into a 43-119s request.

These tests pin the contract: the query count for the list paths is bounded
and independent of the number of rows. They compare a small set against a
larger set for the same endpoint — equal query counts prove O(1)-in-rows.
A separate test pins the response shape so the to_resource FK-id preference
(which avoids the lazy load) doesn't change emitted linkage ids.
"""

import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    CoverLetter,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Resume,
    Score,
    Status,
)

User = get_user_model()


def _make_application(user):
    """A fully-populated application: every to-one FK set + one status row, so
    all the lazy-load paths the N+1 used to exercise are live. Company.name is
    unique=True, so each call mints a globally-unique tag (this also gives the
    JobPost a unique title+company => distinct fingerprint => no dedupe merge)."""
    tag = uuid.uuid4().hex[:10]
    company = Company.objects.create(name=f"CC91Co{tag}")
    job_post = JobPost.objects.create(
        title=f"CC91Role{tag}", company=company, created_by=user
    )
    resume = Resume.objects.create(user=user)
    cover_letter = CoverLetter.objects.create(user=user)
    app = JobApplication.objects.create(
        user=user,
        job_post=job_post,
        company=company,
        resume=resume,
        cover_letter=cover_letter,
        status="applied",
    )
    status = Status.objects.create(status=f"cc91-{tag}")
    JobApplicationStatus.objects.create(application=app, status=status)
    return app


def _make_score(user):
    tag = uuid.uuid4().hex[:10]
    company = Company.objects.create(name=f"CC91SCo{tag}")
    job_post = JobPost.objects.create(
        title=f"CC91SRole{tag}", company=company, created_by=user
    )
    resume = Resume.objects.create(user=user)
    return Score.objects.create(
        user=user, job_post=job_post, resume=resume, score=80, status="completed"
    )


class _QueryCountMixin:
    def _get_counting(self, client, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
            self.assertEqual(resp.status_code, 200, resp.content)
            resp.json()  # force full evaluation/render inside the captured block
        return len(ctx), resp

    def _assert_bounded(self, url_for, small_user, big_user):
        """url_for(user) -> endpoint. Asserts the query count is identical for
        a user with few rows and a user with many — the O(1)-in-rows contract."""
        c_small = APIClient()
        c_small.force_authenticate(user=small_user)
        c_big = APIClient()
        c_big.force_authenticate(user=big_user)
        q_small, r_small = self._get_counting(c_small, url_for(small_user))
        q_big, r_big = self._get_counting(c_big, url_for(big_user))
        self.assertEqual(
            q_small,
            q_big,
            f"N+1 regression on {url_for(big_user)}: {q_small} queries for the "
            f"small set vs {q_big} for the large set — must be equal "
            f"(query count independent of row count).",
        )
        return r_small, r_big


class TestJobApplicationListN1(_QueryCountMixin, TestCase):
    def test_list_query_count_independent_of_row_count(self):
        small = User.objects.create_user(username="cc91_ja_small", password="x")
        for _ in range(2):
            _make_application(small)
        big = User.objects.create_user(username="cc91_ja_big", password="x")
        for _ in range(6):
            _make_application(big)

        r_small, r_big = self._assert_bounded(
            lambda _u: "/api/v1/job-applications/", small, big
        )
        self.assertEqual(len(r_small.json()["data"]), 2)
        self.assertEqual(len(r_big.json()["data"]), 6)

    def test_filter_company_path_bounded(self):
        # The prod 43-119s face of the bug: filter[company]. Confirm it still
        # filters correctly AND issues a bounded number of queries.
        user = User.objects.create_user(username="cc91_ja_filter", password="x")
        apps = [_make_application(user) for _ in range(5)]
        target_name = apps[1].company.name  # unique hex tag => exactly one match
        client = APIClient()
        client.force_authenticate(user=user)
        q, resp = self._get_counting(
            client, f"/api/v1/job-applications/?filter[company]={target_name}"
        )
        self.assertEqual(len(resp.json()["data"]), 1)
        self.assertLess(
            q, 15, f"filter[company] issued {q} queries — expected a small fixed set"
        )


class TestUserRelatedLinkN1(_QueryCountMixin, TestCase):
    def test_user_job_applications_related_link_bounded(self):
        small = User.objects.create_user(username="cc91_uja_small", password="x")
        for _ in range(2):
            _make_application(small)
        big = User.objects.create_user(username="cc91_uja_big", password="x")
        for _ in range(6):
            _make_application(big)
        r_small, r_big = self._assert_bounded(
            lambda u: f"/api/v1/users/{u.id}/job-applications/", small, big
        )
        self.assertEqual(len(r_small.json()["data"]), 2)
        self.assertEqual(len(r_big.json()["data"]), 6)

    def test_user_scores_related_link_bounded(self):
        small = User.objects.create_user(username="cc91_us_small", password="x")
        for _ in range(2):
            _make_score(small)
        big = User.objects.create_user(username="cc91_us_big", password="x")
        for _ in range(6):
            _make_score(big)
        r_small, r_big = self._assert_bounded(
            lambda u: f"/api/v1/users/{u.id}/scores/", small, big
        )
        self.assertEqual(len(r_small.json()["data"]), 2)
        self.assertEqual(len(r_big.json()["data"]), 6)

    def test_user_include_job_applications_bounded(self):
        # The `GET /api/v1/users/<id>?include=job-applications` ~12s path.
        small = User.objects.create_user(username="cc91_inc_small", password="x")
        for _ in range(2):
            _make_application(small)
        big = User.objects.create_user(username="cc91_inc_big", password="x")
        for _ in range(6):
            _make_application(big)
        r_small, r_big = self._assert_bounded(
            lambda u: f"/api/v1/users/{u.id}/?include=job-applications", small, big
        )
        self.assertEqual(len(r_small.json().get("included", [])), 2)
        self.assertEqual(len(r_big.json().get("included", [])), 6)


class TestToResourceLinkagePreserved(TestCase):
    """Guard the to_resource FK-id preference: emitted to-one linkage ids must
    still equal the FK column values, and the linked to-many linkage stays."""

    def test_job_application_relationship_ids_match_fks(self):
        user = User.objects.create_user(username="cc91_link", password="x")
        app = _make_application(user)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(f"/api/v1/job-applications/{app.id}/")
        self.assertEqual(resp.status_code, 200)
        rels = resp.json()["data"]["relationships"]
        self.assertEqual(rels["company"]["data"]["id"], str(app.company_id))
        self.assertEqual(rels["job-post"]["data"]["id"], str(app.job_post_id))
        self.assertEqual(rels["resume"]["data"]["id"], str(app.resume_id))
        self.assertEqual(rels["cover-letter"]["data"]["id"], str(app.cover_letter_id))
        # linked_relationships still emits the application-statuses linkage.
        self.assertTrue(rels["application-statuses"]["data"])

    def test_score_company_property_linkage_preserved(self):
        # Score.company is a @property over job_post.company (no FK column);
        # its linkage must still resolve to the job post's company id.
        user = User.objects.create_user(username="cc91_score_link", password="x")
        score = _make_score(user)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(f"/api/v1/users/{user.id}/scores/")
        self.assertEqual(resp.status_code, 200)
        row = resp.json()["data"][0]
        rels = row["relationships"]
        self.assertEqual(rels["job-post"]["data"]["id"], str(score.job_post_id))
        self.assertEqual(rels["company"]["data"]["id"], str(score.job_post.company_id))
