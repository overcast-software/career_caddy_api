"""BACK-115 — resume `meta.counts` must be O(1) in the number of resumes.

`ResumeSerializer._build_counts()` used to issue four filtered COUNT
queries for every resume it serialized, and `ResumeViewSet.list()`
materialized the user's entire resume table before slicing a page out of
it in Python. A list request with counts therefore cost 1 + 4N queries
over N = the user's total resume count.

These tests pin the two halves of the fix:
  * the query count for a counts-bearing list does not grow with N, and
  * the page is sliced by the database (LIMIT), not in Python.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    Experience,
    JobApplication,
    JobPost,
    Resume,
    ResumeExperience,
    ResumeSkill,
    Score,
    Skill,
)

User = get_user_model()


class BackCountsBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="back115", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Back115Co")

    def _make_resume(self, label, applications=2, scores=1, experiences=1, skills=1):
        """A resume with a known number of rows behind each meta count."""
        resume = Resume.objects.create(user=self.user, title=label)
        for i in range(applications):
            JobApplication.objects.create(
                job_post=self._job_post(f"{label}-app-{i}"),
                resume=resume,
                user=self.user,
            )
        for i in range(scores):
            # unique_score_per_job_resume_user -> one score per job post.
            Score.objects.create(
                job_post=self._job_post(f"{label}-score-{i}"),
                resume=resume,
                user=self.user,
                score=70 + i,
            )
        for i in range(experiences):
            ResumeExperience.objects.create(
                resume=resume,
                experience=Experience.objects.create(title=f"{label}-exp-{i}"),
                order=i,
            )
        for i in range(skills):
            ResumeSkill.objects.create(
                resume=resume,
                skill=Skill.objects.create(text=f"{label}-skill-{i}"),
            )
        return resume

    def _job_post(self, title):
        return JobPost.objects.create(
            title=title, company=self.company, created_by=self.user
        )

    def _query_count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)


class TestResumeCountsQueryCountIsConstant(BackCountsBase):
    """The gate: the query count must not track the number of resumes."""

    def _assert_constant_across_n(self, url):
        for i in range(2):
            self._make_resume(f"small-{i}")
        with_two = self._query_count(url)

        for i in range(6):
            self._make_resume(f"big-{i}")
        with_eight = self._query_count(url)

        self.assertEqual(
            with_two,
            with_eight,
            f"{url} is O(N): {with_two} queries for 2 resumes, "
            f"{with_eight} for 8",
        )

    def test_slim_list_query_count_does_not_grow_with_resume_count(self):
        self._assert_constant_across_n("/api/v1/resumes/?slim=true")

    def test_meta_counts_adds_no_queries_to_a_list(self):
        """`?meta=counts` must be free in query terms.

        The non-slim list still sideloads six relationships per
        `_default_includes` (its own N+1, tracked separately by the
        slim -> JSON:API migration), so the meaningful measure here is the
        *delta* the counts opt-in adds. Before BACK-115 that delta was 4N.
        """
        for i in range(8):
            self._make_resume(f"delta-{i}")
        without_counts = self._query_count("/api/v1/resumes/?fields[resume]=name")
        with_counts = self._query_count(
            "/api/v1/resumes/?fields[resume]=name&meta=counts"
        )
        self.assertEqual(
            with_counts,
            without_counts,
            f"meta=counts added {with_counts - without_counts} queries over "
            "8 resumes; it must add none",
        )

    def test_slim_list_issues_a_single_query(self):
        for i in range(5):
            self._make_resume(f"one-query-{i}")
        with self.assertNumQueries(1):
            response = self.client.get("/api/v1/resumes/?slim=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 5)


class TestResumeCountsValuesUnchanged(BackCountsBase):
    """Acceptance: the emitted numbers are the same as before the fix."""

    def test_counts_are_per_resume_and_correct(self):
        a = self._make_resume("A", applications=2, scores=1, experiences=3, skills=4)
        b = self._make_resume("B", applications=0, scores=2, experiences=1, skills=0)
        empty = Resume.objects.create(user=self.user, title="Empty")

        body = self.client.get("/api/v1/resumes/?slim=true").json()
        meta = {r["id"]: r["meta"] for r in body["data"]}

        self.assertEqual(
            meta[str(a.id)],
            {
                "job_application_count": 2,
                "score_count": 1,
                "experience_count": 3,
                "skill_count": 4,
            },
        )
        self.assertEqual(
            meta[str(b.id)],
            {
                "job_application_count": 0,
                "score_count": 2,
                "experience_count": 1,
                "skill_count": 0,
            },
        )
        self.assertEqual(
            meta[str(empty.id)],
            {
                "job_application_count": 0,
                "score_count": 0,
                "experience_count": 0,
                "skill_count": 0,
            },
        )

    def test_meta_counts_param_matches_slim_counts(self):
        self._make_resume("Same", applications=3, scores=2, experiences=2, skills=5)
        slim = self.client.get("/api/v1/resumes/?slim=true").json()["data"][0]["meta"]
        explicit = self.client.get(
            "/api/v1/resumes/?fields[resume]=name&meta=counts"
        ).json()["data"][0]["meta"]
        self.assertEqual(slim, explicit)

    def test_retrieve_still_counts_without_annotation(self):
        """`retrieve` never annotates — the per-row fallback must stay."""
        resume = self._make_resume(
            "Detail", applications=1, scores=2, experiences=3, skills=1
        )
        body = self.client.get(f"/api/v1/resumes/{resume.id}/?meta=counts").json()
        self.assertEqual(
            body["data"]["meta"],
            {
                "job_application_count": 1,
                "score_count": 2,
                "experience_count": 3,
                "skill_count": 1,
            },
        )


class TestResumeListPaginatesInDatabase(BackCountsBase):
    def test_page_is_sliced_by_the_database(self):
        for i in range(3):
            self._make_resume(f"page-{i}")

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/v1/resumes/?slim=true&page[size]=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 1)
        # The outer statement must carry the LIMIT. (The correlated count
        # subqueries each end in `LIMIT 1` too, so anchor on the tail of
        # the whole statement rather than a substring search.)
        limited = [
            q["sql"]
            for q in ctx.captured_queries
            if 'FROM "resume"' in q["sql"] and q["sql"].rstrip().endswith("LIMIT 1")
        ]
        self.assertTrue(
            limited,
            "the resume page was materialized in Python, not LIMITed in SQL: "
            + repr([q["sql"] for q in ctx.captured_queries]),
        )
