"""CC #51 — public (AllowAny) federated-job-posts endpoint tests.

Pins the contract of
``GET /api/v1/users/<username>/job-posts/federated/``:

* public, no auth — anonymous client gets 200
* returns ONLY the named user's audience-public posts
* excludes the user's private ([]) and non-public-audience posts
* excludes other users' public posts
* unknown username → 404
* known user with no public posts → 200 + empty list
* payload carries the public projection ONLY — no private/owner fields,
  no relationships, no sideloaded ``included``
* trailing slash is optional (router dual-slash convention)
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from job_hunting.models import Company, JobPost, Score
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()

URL = "/api/v1/users/{username}/job-posts/federated/"

# Fields the public projection must NEVER leak. Mix of owner/private,
# dedupe-pipeline internals, federation internals, and per-user signals.
_FORBIDDEN_ATTRS = {
    "source",
    "audience",
    "canonical_link",
    "content_fingerprint",
    "normalized_fingerprint",
    "duplicate_of_id",
    "reposted_from_id",
    "complete",
    "extraction_date",
    "apply_url_status",
    "apply_url_resolved_at",
    "source_instance",
    "source_deleted_at",
    "top_score",
    "created_by",
    "created_by_id",
}


class TestPublicFederatedJobPosts(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="dough", password="pass")
        self.other = User.objects.create_user(username="judge", password="pass")
        # A real user who has never published anything — the empty-profile case.
        self.bare = User.objects.create_user(username="spaulding", password="pass")
        self.company = Company.objects.create(name="Bushwood CC")

        self.public_post = JobPost.objects.create(
            created_by=self.owner,
            title="Senior Greenskeeper",
            description="Tend the greens",
            link="https://example.com/jobs/1",
            location="Bushwood",
            company=self.company,
            audience=[AS2_PUBLIC],
        )
        self.private_post = JobPost.objects.create(
            created_by=self.owner,
            title="Private musings",
            description="Not for the world",
            link="https://example.com/jobs/2",
            audience=[],
        )
        # Has an audience, but NOT the Public URI — followers-only never
        # federates to the public profile (the filter is membership of
        # AS2_PUBLIC specifically, not "non-empty audience").
        self.followers_only_post = JobPost.objects.create(
            created_by=self.owner,
            title="Followers-only role",
            link="https://example.com/jobs/3",
            audience=["https://example.com/actors/dough/followers"],
        )
        self.other_public = JobPost.objects.create(
            created_by=self.other,
            title="Someone else's public role",
            link="https://example.com/jobs/4",
            audience=[AS2_PUBLIC],
        )
        # Per-user signal that JobPostSerializer would surface as top_score
        # — must not appear on the public projection.
        Score.objects.create(
            job_post=self.public_post, user=self.owner, score=99
        )

    def test_anonymous_gets_only_owner_public_posts(self):
        resp = self.client.get(URL.format(username="dough"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        ids = {item["id"] for item in body["data"]}
        self.assertEqual(ids, {str(self.public_post.id)})

    def test_excludes_private_nonpublic_and_other_users(self):
        body = self.client.get(URL.format(username="dough")).json()
        ids = {item["id"] for item in body["data"]}
        self.assertNotIn(str(self.private_post.id), ids)
        self.assertNotIn(str(self.followers_only_post.id), ids)
        self.assertNotIn(str(self.other_public.id), ids)

    def test_resource_shape_is_public_projection(self):
        body = self.client.get(URL.format(username="dough")).json()
        item = body["data"][0]
        self.assertEqual(item["type"], "public-job-post")
        self.assertEqual(item["id"], str(self.public_post.id))
        attrs = item["attributes"]
        self.assertEqual(attrs["title"], "Senior Greenskeeper")
        self.assertEqual(attrs["description"], "Tend the greens")
        self.assertEqual(attrs["location"], "Bushwood")
        self.assertEqual(attrs["company_name"], "Bushwood CC")
        # No relationships block, no sideloaded resources.
        self.assertNotIn("relationships", item)
        self.assertNotIn("included", body)

    def test_no_private_fields_leak(self):
        body = self.client.get(URL.format(username="dough")).json()
        for item in body["data"]:
            leaked = _FORBIDDEN_ATTRS & set(item["attributes"].keys())
            self.assertEqual(leaked, set(), f"leaked private fields: {leaked}")
            self.assertNotIn("meta", item)  # no per-caller triage meta

    def test_unknown_user_is_404(self):
        resp = self.client.get(URL.format(username="nobody"))
        self.assertEqual(resp.status_code, 404)

    def test_known_user_no_public_posts_is_empty_200(self):
        resp = self.client.get(URL.format(username="spaulding"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["data"], [])
        self.assertEqual(body["meta"]["total"], 0)

    def test_trailing_slash_optional(self):
        with_slash = self.client.get("/api/v1/users/dough/job-posts/federated/")
        no_slash = self.client.get("/api/v1/users/dough/job-posts/federated")
        self.assertEqual(with_slash.status_code, 200)
        self.assertEqual(no_slash.status_code, 200)

    def test_ordering_newest_first(self):
        newer = JobPost.objects.create(
            created_by=self.owner,
            title="Fresher public role",
            link="https://example.com/jobs/5",
            audience=[AS2_PUBLIC],
        )
        body = self.client.get(URL.format(username="dough")).json()
        ids = [item["id"] for item in body["data"]]
        self.assertEqual(ids[0], str(newer.id))
