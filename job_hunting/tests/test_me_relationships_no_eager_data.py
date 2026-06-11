"""Regression tests for the JSON:API conformance of /api/v1/me/.

Before this fix, DjangoUserSerializer eagerly populated `data: [...]`
for every relationship — scores, job-applications, resumes, summaries,
cover-letters. For active users the scores linkage alone is 280+
resource identifiers, which inflated the /me/ payload to hundreds of
KB and contributed to MCP container memory pressure (the verifier
cached the full /me/ response in AccessToken.claims["user"]).

JSON:API spec: relationships objects hold `links` (and optionally
`meta`) by default; `data` is a compound-document concern, populated
only when the client opts in via `?include=...`.

These tests pin the corrected shape on both the dedicated `/me/`
endpoint and the underlying `users/<id>/` retrieve + list paths,
since all three share the DjangoUserSerializer code path.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Score

User = get_user_model()
ME_URL = "/api/v1/me/"


class TestMeRelationshipsNoEagerData(TestCase):
    """/me/ default response: relationships have links, no data array."""

    REL_NAMES = (
        "resumes",
        "scores",
        "cover-letters",
        "job-applications",
        "summaries",
    )

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="docnotin", password="pw", first_name="Doc", last_name="In"
        )
        self.client.force_authenticate(user=self.user)
        # Seed Score rows so the bug shape (linkage_data: [...]) would
        # be observable if it regressed.
        company = Company.objects.create(name="Acme")
        self.jp_a = JobPost.objects.create(
            title="Engineer A", company=company, created_by=self.user,
            description="x " * 80,
        )
        self.jp_b = JobPost.objects.create(
            title="Engineer B", company=company, created_by=self.user,
            description="x " * 80,
        )
        # One Score per JobPost — the model carries a unique constraint
        # on (job_post, user) so two scores on the same JP would 500.
        Score.objects.create(job_post=self.jp_a, user=self.user, score=80)
        Score.objects.create(job_post=self.jp_b, user=self.user, score=85)

    def _relationships(self, resp):
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        return body["data"]["relationships"], body

    def test_me_returns_links_only_relationships_by_default(self):
        """Every declared relationship must have `links` and must NOT
        carry a `data` array on the default GET /me/ response."""
        rels, _body = self._relationships(self.client.get(ME_URL))

        for name in self.REL_NAMES:
            with self.subTest(rel=name):
                self.assertIn(name, rels, f"missing {name} relationship")
                rel = rels[name]
                self.assertIn("links", rel, f"{name} missing links block")
                self.assertIn(
                    "self", rel["links"], f"{name} links.self missing"
                )
                self.assertIn(
                    "related", rel["links"], f"{name} links.related missing"
                )
                self.assertNotIn(
                    "data",
                    rel,
                    f"{name} must not carry `data` linkage without ?include=",
                )

    def test_me_default_response_has_no_top_level_included(self):
        """No `?include=` means no compound document."""
        _rels, body = self._relationships(self.client.get(ME_URL))
        self.assertNotIn("included", body)

    def test_me_with_include_scores_populates_data_and_included(self):
        """?include=scores activates the JSON:API compound-document
        contract: relationships.scores.data lists the linkage AND
        top-level included[] holds the score resources."""
        rels, body = self._relationships(
            self.client.get(f"{ME_URL}?include=scores")
        )
        scores_rel = rels["scores"]
        self.assertIn("data", scores_rel, "scores.data must populate on include")
        ids_in_linkage = {item["id"] for item in scores_rel["data"]}
        types_in_linkage = {item["type"] for item in scores_rel["data"]}
        self.assertEqual(types_in_linkage, {"score"})
        self.assertEqual(len(ids_in_linkage), 2)

        # Other relationships still links-only because they weren't
        # named in ?include=.
        for other in ("resumes", "cover-letters", "job-applications", "summaries"):
            with self.subTest(rel=other):
                self.assertNotIn("data", rels[other])

        self.assertIn("included", body)
        included_scores = [r for r in body["included"] if r["type"] == "score"]
        self.assertEqual({r["id"] for r in included_scores}, ids_in_linkage)

    def test_me_with_multiple_includes_gates_each_relationship(self):
        """A multi-include must turn `data` on for each named rel and
        leave the others as links-only."""
        rels, body = self._relationships(
            self.client.get(f"{ME_URL}?include=scores,job-applications")
        )
        self.assertIn("data", rels["scores"])
        self.assertIn("data", rels["job-applications"])
        for other in ("resumes", "cover-letters", "summaries"):
            with self.subTest(rel=other):
                self.assertNotIn("data", rels[other])

    def test_me_tolerates_underscore_include_variant(self):
        """?include=job_applications (underscore) maps to job-applications."""
        rels, _body = self._relationships(
            self.client.get(f"{ME_URL}?include=job_applications")
        )
        self.assertIn("data", rels["job-applications"])


class TestUsersRetrieveRelationshipsShape(TestCase):
    """The /me/ fix lives in DjangoUserSerializer, so it must also
    apply to GET /api/v1/users/<id>/ — the path the frontend's
    `store.findRecord('user', id)` actually walks."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="storefind", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_retrieve_self_has_no_eager_data_arrays(self):
        resp = self.client.get(f"/api/v1/users/{self.user.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rels = resp.json()["data"]["relationships"]
        for name in (
            "resumes",
            "scores",
            "cover-letters",
            "job-applications",
            "summaries",
        ):
            with self.subTest(rel=name):
                self.assertNotIn("data", rels[name])
                self.assertIn("links", rels[name])


class TestUsersListRelationshipsShape(TestCase):
    """Non-staff list returns [self] — same DjangoUserSerializer path,
    same JSON:API contract. Staff get the full list; check both."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ordone", password="pw")
        self.staff = User.objects.create_user(
            username="staffone", password="pw", is_staff=True
        )

    def _assert_all_links_only(self, payload):
        for resource in payload["data"]:
            for name, rel in resource["relationships"].items():
                with self.subTest(user=resource["id"], rel=name):
                    self.assertNotIn(
                        "data",
                        rel,
                        f"{name} on user {resource['id']} must be links-only",
                    )
                    self.assertIn("links", rel)

    def test_non_staff_list_is_links_only(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/users/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self._assert_all_links_only(resp.json())

    def test_staff_list_is_links_only(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/users/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self._assert_all_links_only(resp.json())
