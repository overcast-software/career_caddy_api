"""Phase 4 ActivityPub-prep: /as-object/ adapter + source_instance.

These tests pin the AS2 JSON-LD contract emitted by the adapter. The
shape is checked structurally rather than against a JSON-LD validator
because the only consumers right now are local tests + the Phase 5
federation worker; once we federate to a real peer the validator suite
becomes worth the dep weight.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.lib.as_object import (
    AS2_CONTEXT,
    actor_uri,
    job_post_as_object,
    object_uri,
)
from job_hunting.models import Company, JobPost, JobPostDiscovery
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


class TestSourceInstanceDefault(TestCase):
    """source_instance backfills to settings.CAREER_CADDY_INSTANCE so
    every existing row carries this instance's hostname. Federated rows
    arriving later set source_instance to the remote host."""

    def setUp(self):
        self.user = User.objects.create_user(username="si_user", password="pass")

    def test_source_instance_defaults_to_local(self):
        jp = JobPost.objects.create(title="Local", created_by=self.user)
        jp.refresh_from_db()
        self.assertEqual(jp.source_instance, settings.CAREER_CADDY_INSTANCE)

    @override_settings(
        CAREER_CADDY_INSTANCE="other.example.test",
        INSTANCE_ORIGIN="https://other.example.test",
    )
    def test_default_resolves_setting_at_create_time(self):
        """Callable default re-evaluates per create — important for
        tests that override settings and for the rare same-binary
        multi-instance dev setup."""
        jp = JobPost.objects.create(title="Other", created_by=self.user)
        jp.refresh_from_db()
        self.assertEqual(jp.source_instance, "other.example.test")

    def test_federated_row_persists_remote_host(self):
        jp = JobPost.objects.create(
            title="Federated",
            created_by=self.user,
            source_instance="peer.example.test",
        )
        jp.refresh_from_db()
        self.assertEqual(jp.source_instance, "peer.example.test")


class TestAsObjectAdapter(TestCase):
    """The Python-level adapter; HTTP wiring is exercised below."""

    def setUp(self):
        self.user = User.objects.create_user(username="adapter_user", password="pass")
        self.company = Company.objects.create(name="AdapterCo")
        self.jp = JobPost.objects.create(
            title="Senior Engineer",
            description="Build the thing.",
            company=self.company,
            link="https://example.com/jobs/42",
            location="Remote",
            apply_url="https://ats.example.com/apply/42",
            created_by=self.user,
            audience=[AS2_PUBLIC],
        )

    def test_basic_shape(self):
        obj = job_post_as_object(self.jp)
        self.assertEqual(obj["type"], "Note")
        self.assertEqual(obj["id"], object_uri(self.jp))
        self.assertEqual(
            obj["attributedTo"],
            actor_uri("adapter_user", self.jp.source_instance),
        )
        self.assertEqual(obj["name"], "Senior Engineer")
        self.assertEqual(obj["content"], "Build the thing.")
        self.assertEqual(obj["url"], "https://example.com/jobs/42")

    def test_context_includes_as2_and_extension(self):
        obj = job_post_as_object(self.jp)
        ctx = obj["@context"]
        self.assertIn(AS2_CONTEXT, ctx)
        # Custom namespace is reachable as a prefix definition.
        ns_block = next((c for c in ctx if isinstance(c, dict)), None)
        self.assertIsNotNone(ns_block)
        self.assertIn("careercaddy", ns_block)

    def test_public_audience_emits_to_and_audience(self):
        obj = job_post_as_object(self.jp)
        self.assertEqual(obj["to"], [AS2_PUBLIC])
        self.assertEqual(obj["audience"], [AS2_PUBLIC])

    def test_private_audience_omits_to_and_audience(self):
        self.jp.audience = []
        self.jp.save()
        obj = job_post_as_object(self.jp)
        self.assertNotIn("to", obj)
        self.assertNotIn("audience", obj)

    def test_extension_carries_career_caddy_fields(self):
        obj = job_post_as_object(self.jp)
        ext = obj["careercaddy:extension"]
        self.assertEqual(ext["source"], "manual")
        self.assertEqual(ext["sourceInstance"], self.jp.source_instance)
        self.assertEqual(ext["applyUrl"], "https://ats.example.com/apply/42")
        self.assertEqual(ext["canonicalLink"], "https://example.com/jobs/42")
        self.assertEqual(ext["company"], "AdapterCo")

    def test_omits_null_optional_fields(self):
        """Empty / None values must not appear as JSON null — AS2
        clients (Mastodon, Lemmy) reject string-typed fields with null."""
        jp = JobPost.objects.create(created_by=self.user)
        obj = job_post_as_object(jp)
        self.assertNotIn("name", obj)
        self.assertNotIn("content", obj)
        self.assertNotIn("url", obj)
        self.assertNotIn("location", obj)

    def test_object_uri_uses_source_instance_not_local(self):
        """A federated row keeps its origin URI even when re-emitted
        through this instance — peers identify objects by origin."""
        jp = JobPost.objects.create(
            title="From Peer",
            created_by=self.user,
            source_instance="peer.example.test",
        )
        self.assertEqual(
            object_uri(jp), f"https://peer.example.test/job-posts/{jp.pk}"
        )

    def test_published_falls_back_to_created_at(self):
        """posted_date may be unset on paste/scrape rows; the adapter
        falls back to created_at so federation peers always get a
        timestamp."""
        jp = JobPost.objects.create(title="No date", created_by=self.user)
        # JobPost.save() backfills posted_date to today, so isolate
        # the fallback by clearing it post-save.
        JobPost.objects.filter(pk=jp.pk).update(posted_date=None)
        jp.refresh_from_db()
        obj = job_post_as_object(jp)
        self.assertIn("published", obj)


class TestAsObjectEndpoint(TestCase):
    """HTTP wiring + visibility scoping for /as-object/.

    Visibility-scoping is the load-bearing piece: the adapter shouldn't
    leak job posts the caller couldn't otherwise see, even though the
    payload is described as 'federation-safe'."""

    def setUp(self):
        self.owner = User.objects.create_user(username="ao_owner", password="pass")
        self.stranger = User.objects.create_user(username="ao_stranger", password="pass")
        self.staff = User.objects.create_user(
            username="ao_staff", password="pass", is_staff=True
        )
        self.company = Company.objects.create(name="HttpCo")
        self.jp = JobPost.objects.create(
            title="Visible",
            company=self.company,
            link="https://example.com/visible",
            created_by=self.owner,
        )

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_owner_sees_as_object(self):
        response = self._client(self.owner).get(
            f"/api/v1/job-posts/{self.jp.id}/as-object/"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "Note")
        self.assertEqual(body["name"], "Visible")

    def test_stranger_gets_404(self):
        response = self._client(self.stranger).get(
            f"/api/v1/job-posts/{self.jp.id}/as-object/"
        )
        self.assertEqual(response.status_code, 404)

    def test_staff_sees_any_jp(self):
        response = self._client(self.staff).get(
            f"/api/v1/job-posts/{self.jp.id}/as-object/"
        )
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_blocked(self):
        response = APIClient().get(
            f"/api/v1/job-posts/{self.jp.id}/as-object/"
        )
        self.assertEqual(response.status_code, 401)


class TestVisibilityAuditFederatedRows(TestCase):
    """Phase 4 visibility audit: confirm the five-clause filter
    (created / applied / scored / scraped / discovered) keeps federated
    rows invisible by default, and that an explicit Discovery promotes
    them to visible — Discovery is the subscription primitive."""

    def setUp(self):
        self.user = User.objects.create_user(username="vf_user", password="pass")
        self.peer_owner = User.objects.create_user(
            username="vf_peer_owner", password="pass"
        )
        self.staff = User.objects.create_user(
            username="vf_staff", password="pass", is_staff=True
        )
        # Federated row: created by someone else on a different instance.
        # Local user has no signal on it whatsoever.
        self.federated_jp = JobPost.objects.create(
            title="Remote",
            link="https://peer.example.test/job/42",
            created_by=self.peer_owner,
            source_instance="peer.example.test",
        )

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_federated_row_invisible_in_list(self):
        response = self._client(self.user).get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, 200)
        ids = {int(item["id"]) for item in response.json()["data"]}
        self.assertNotIn(self.federated_jp.id, ids)

    def test_federated_row_invisible_on_retrieve(self):
        response = self._client(self.user).get(
            f"/api/v1/job-posts/{self.federated_jp.id}/"
        )
        # 404 because list visibility filter excludes it — same shape as
        # any other "not in your scope" 404 from this viewset.
        self.assertIn(response.status_code, (403, 404))

    def test_discovery_promotes_federated_row_to_visible(self):
        """Discovery is the subscription primitive: a local user with a
        JobPostDiscovery for a federated row can see it. This is the
        opt-in semantics the plan promised."""
        JobPostDiscovery.objects.create(
            job_post=self.federated_jp,
            user=self.user,
            source="email-forward",
        )
        response = self._client(self.user).get("/api/v1/job-posts/")
        ids = {int(item["id"]) for item in response.json()["data"]}
        self.assertIn(self.federated_jp.id, ids)

    def test_staff_sees_federated_row(self):
        response = self._client(self.staff).get("/api/v1/job-posts/")
        ids = {int(item["id"]) for item in response.json()["data"]}
        self.assertIn(self.federated_jp.id, ids)
