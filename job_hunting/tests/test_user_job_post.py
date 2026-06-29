"""BACK-105 — UserJobPost owner↔post join (AUTO-18 multi-user forward@).

cc_auto (a STAFF api key) ingests job mail forwarded to
``<username>@careercaddy.online`` and records the OWNER = the resolved
recipient user via a UserJobPost row, SEPARATE from ``created_by`` (which
stays the author/staff principal that drove the write). Covers:

- staff POST owner_user_id=X (X != principal) → UserJobPost(user=X,
  role="owner") AND X sees the post via list + detail
- non-staff POST owner_user_id=other → 403, no row written
- absent owner_user_id → no UserJobPost row (post still created)
- re-POST the SAME link with the same owner → idempotent ((job_post, user)
  unique holds; no duplicate row)
- created_by is ALWAYS the principal — owner attribution never moves it
- owner recorded on the dedup/merge-return path too, not just fresh insert
- stale / non-integer owner_user_id → 400
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, UserJobPost


User = get_user_model()


class UserJobPostOwnerTests(TestCase):
    def setUp(self):
        # cc_auto is the STAFF api-key principal that drives on-behalf writes.
        self.cc_auto = User.objects.create_user(
            username="cc-auto", password="p", is_staff=True
        )
        self.dough = User.objects.create_user(username="dough", password="p")
        self.target = User.objects.create_user(username="target", password="p")
        self.company = Company.objects.create(name="Acme")

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _payload(self, link, **extra):
        attrs = {
            "title": "Engineer",
            "link": link,
            "description": "x" * 500,
            **extra,
        }
        return {"data": {"type": "job-post", "attributes": attrs}}

    def _list_ids(self, user):
        resp = self._client(user).get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        return {r["id"] for r in resp.json()["data"]}

    def test_staff_records_owner_for_target(self):
        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/owner-fresh",
                owner_user_id=self.target.id,
                source="email-forward",
                forwarded_via_address="target@careercaddy.online",
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post_id = resp.json()["data"]["id"]

        ujp = UserJobPost.objects.get(job_post_id=post_id, user=self.target)
        self.assertEqual(ujp.role, "owner")
        self.assertEqual(ujp.source, "email-forward")

        # created_by stays the staff principal — ownership never moves it.
        self.assertEqual(
            JobPost.objects.get(id=post_id).created_by_id, self.cc_auto.id
        )

    def test_owner_sees_post_in_list_and_detail(self):
        # The target's ONLY signal is the UserJobPost membership (no
        # discovery, no created_by) — the additive read-scoping leg must
        # surface the post in both list and detail.
        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/owner-visible",
                owner_user_id=self.target.id,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post_id = resp.json()["data"]["id"]

        self.assertIn(post_id, self._list_ids(self.target))
        detail = self._client(self.target).get(f"/api/v1/job-posts/{post_id}/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["id"], post_id)

    def test_non_staff_owner_on_behalf_403(self):
        resp = self._client(self.dough).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/owner-403",
                owner_user_id=self.target.id,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(
            UserJobPost.objects.filter(user=self.target).exists(),
            "403 must reject before any DB write",
        )

    def test_self_owner_non_staff_ok(self):
        # Non-staff targeting self is allowed (RBAC permits self-target).
        resp = self._client(self.dough).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/owner-self",
                owner_user_id=self.dough.id,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post_id = resp.json()["data"]["id"]
        ujp = UserJobPost.objects.get(job_post_id=post_id, user=self.dough)
        self.assertEqual(ujp.role, "owner")
        # Self-owner: created_by and owner happen to coincide, but they are
        # set by independent code paths — created_by is still the principal.
        self.assertEqual(
            JobPost.objects.get(id=post_id).created_by_id, self.dough.id
        )

    def test_absent_owner_user_id_writes_no_row(self):
        resp = self._client(self.dough).post(
            "/api/v1/job-posts/",
            self._payload("https://acme.example/jobs/no-owner"),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post_id = resp.json()["data"]["id"]
        self.assertFalse(
            UserJobPost.objects.filter(job_post_id=post_id).exists(),
            "no owner_user_id → no UserJobPost row",
        )
        self.assertEqual(
            JobPost.objects.get(id=post_id).created_by_id, self.dough.id
        )

    def test_repeat_post_same_owner_is_idempotent(self):
        link = "https://acme.example/jobs/owner-idempotent"
        first = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(link, owner_user_id=self.target.id),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)
        post_id = first.json()["data"]["id"]

        # Re-POST the same link with the same owner → link-dedupe (200 echo).
        second = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(link, owner_user_id=self.target.id),
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["data"]["id"], post_id)

        self.assertEqual(
            UserJobPost.objects.filter(
                job_post_id=post_id, user=self.target
            ).count(),
            1,
            "(job_post, user) unique holds — no duplicate owner row",
        )

    def test_owner_recorded_on_link_dedupe_return(self):
        # Owner must be recorded even when the JobPost already exists (the
        # shared-link / cc_auto re-POST case), mirroring _record_discovery.
        link = "https://acme.example/jobs/owner-dedupe"
        existing = JobPost.objects.create(
            title="SRE",
            company=self.company,
            link=link,
            description="y" * 500,
            created_by=self.dough,
        )
        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(link, owner_user_id=self.target.id),
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["id"], existing.id)

        ujp = UserJobPost.objects.get(job_post_id=existing.id, user=self.target)
        self.assertEqual(ujp.role, "owner")
        # created_by on the pre-existing row is untouched by ownership.
        existing.refresh_from_db()
        self.assertEqual(existing.created_by_id, self.dough.id)

    def test_stale_owner_user_id_400(self):
        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/owner-stale",
                owner_user_id=99999,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("does not exist", resp.json()["errors"][0]["detail"])

    def test_non_integer_owner_user_id_400(self):
        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/owner-badid",
                owner_user_id="not-an-int",
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("integer", resp.json()["errors"][0]["detail"].lower())

    def test_dasherized_owner_user_id_accepted(self):
        # Ember / JSON:API clients dasherize attribute keys; the create
        # path accepts owner-user-id as well as owner_user_id.
        payload = self._payload("https://acme.example/jobs/owner-dasher")
        payload["data"]["attributes"]["owner-user-id"] = self.target.id
        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/", payload, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post_id = resp.json()["data"]["id"]
        self.assertTrue(
            UserJobPost.objects.filter(
                job_post_id=post_id, user=self.target, role="owner"
            ).exists()
        )

    def test_discover_for_user_id_still_works_alongside_owner(self):
        # Coexistence guard: discover_for_user_id (visibility-signal row)
        # and owner_user_id (ownership join) are independent fields. A staff
        # POST carrying both writes a discovery for one target and an owner
        # membership for the other, and created_by stays the principal.
        from job_hunting.models import JobPostDiscovery

        resp = self._client(self.cc_auto).post(
            "/api/v1/job-posts/",
            self._payload(
                "https://acme.example/jobs/both-fields",
                discover_for_user_id=self.dough.id,
                owner_user_id=self.target.id,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        post_id = resp.json()["data"]["id"]

        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post_id=post_id, user=self.dough
            ).exists()
        )
        self.assertTrue(
            UserJobPost.objects.filter(
                job_post_id=post_id, user=self.target, role="owner"
            ).exists()
        )
        self.assertEqual(
            JobPost.objects.get(id=post_id).created_by_id, self.cc_auto.id
        )
