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
        self.assertEqual(item["type"], "job-post")
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


USER_URL = "/api/v1/users/{username}/"

# User+Profile fields the public `user` resource must NEVER leak.
_FORBIDDEN_USER_ATTRS = {
    "email",
    "is_staff",
    "is_active",
    "is_guest",
    "phone",
    "address",
    "links",
    "password",
    "onboarding",
    "auto_score",
    "linkedin",
    "github",
}


class TestPublicUserProfile(TestCase):
    """CC #51 — public ``user`` resource at GET /api/v1/users/<username>/."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="dough", password="pass", first_name="Ty", last_name="Webb"
        )
        self.other = User.objects.create_user(username="judge", password="pass")
        # A real user with no display name and no posts — fallback + empty case.
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
            link="https://example.com/jobs/2",
            audience=[],
        )
        self.other_public = JobPost.objects.create(
            created_by=self.other,
            title="Someone else's public role",
            link="https://example.com/jobs/4",
            audience=[AS2_PUBLIC],
        )

    def test_user_resource_is_canonical_id_and_public_safe(self):
        resp = self.client.get(USER_URL.format(username="dough"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["type"], "user")
        # id is the canonical numeric id, NOT the username.
        self.assertEqual(data["id"], str(self.owner.id))
        self.assertNotEqual(data["id"], "dough")
        attrs = data["attributes"]
        self.assertEqual(attrs["username"], "dough")
        self.assertEqual(attrs["display_name"], "Ty Webb")
        leaked = _FORBIDDEN_USER_ATTRS & set(attrs.keys())
        self.assertEqual(leaked, set(), f"leaked private user fields: {leaked}")

    def test_display_name_falls_back_to_username(self):
        data = self.client.get(USER_URL.format(username="spaulding")).json()["data"]
        self.assertEqual(data["attributes"]["display_name"], "spaulding")

    def test_federated_relationship_link_only_without_include(self):
        data = self.client.get(USER_URL.format(username="dough")).json()
        rel = data["data"]["relationships"]["federated"]
        self.assertEqual(
            rel["links"]["related"],
            "/api/v1/users/dough/job-posts/federated/",
        )
        # link-only: no data linkage, no sideload, when ?include is absent.
        self.assertNotIn("data", rel)
        self.assertNotIn("included", data)

    def test_include_federated_sideloads_public_job_posts(self):
        body = self.client.get(
            USER_URL.format(username="dough") + "?include=federated"
        ).json()
        rel = body["data"]["relationships"]["federated"]
        # still carries the related link.
        self.assertEqual(
            rel["links"]["related"],
            "/api/v1/users/dough/job-posts/federated/",
        )
        # data linkage points only at the owner's public post.
        self.assertEqual(rel["data"], [{"type": "job-post", "id": str(self.public_post.id)}])
        # top-level included carries the public job-post resource(s).
        self.assertIn("included", body)
        inc_types = {r["type"] for r in body["included"]}
        self.assertEqual(inc_types, {"job-post"})
        inc_ids = {r["id"] for r in body["included"]}
        self.assertEqual(inc_ids, {str(self.public_post.id)})
        # private + other-user posts never sideload.
        self.assertNotIn(str(self.private_post.id), inc_ids)
        self.assertNotIn(str(self.other_public.id), inc_ids)

    def test_unknown_user_is_404(self):
        resp = self.client.get(USER_URL.format(username="nobody"))
        self.assertEqual(resp.status_code, 404)

    def test_trailing_slash_optional(self):
        with_slash = self.client.get("/api/v1/users/dough/")
        no_slash = self.client.get("/api/v1/users/dough")
        self.assertEqual(with_slash.status_code, 200)
        self.assertEqual(no_slash.status_code, 200)

    def test_numeric_id_does_not_hit_public_route(self):
        # A purely-numeric segment falls through to the authed numeric-pk
        # retrieve route (IsAuthenticated) — the public AllowAny view must
        # NOT capture it, so an anonymous client is rejected, not served a
        # public projection.
        resp = self.client.get(f"/api/v1/users/{self.owner.id}/")
        self.assertIn(resp.status_code, (401, 403))
