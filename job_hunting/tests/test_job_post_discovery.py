"""JobPost is shared; per-user visibility flows through JobPostDiscovery.

Covers:
- Non-staff list view hides posts the caller has no signal on.
- Discovery alone is enough to surface a post in the list.
- Staff bypass: every JobPost shows regardless of relation.
- POST creates a Discovery on every branch (fresh, link-dedupe, fingerprint-dedupe).
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, JobPostDiscovery


User = get_user_model()


class JobPostDiscoveryListTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="p")
        self.bob = User.objects.create_user(username="bob", password="p")
        self.company = Company.objects.create(name="Acme")
        # Created by alice — bob has no signal.
        self.post = JobPost.objects.create(
            title="SRE",
            company=self.company,
            link="https://acme.example/jobs/sre",
            description="x" * 500,
            created_by=self.alice,
        )

    def _ids(self, resp):
        return {int(r["id"]) for r in resp.json()["data"]}

    def test_non_staff_without_signal_does_not_see_post(self):
        client = APIClient()
        client.force_authenticate(user=self.bob)
        resp = client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.post.id, self._ids(resp))

    def test_discovery_surfaces_post_for_non_staff(self):
        JobPostDiscovery.objects.create(
            job_post=self.post, user=self.bob, source="email"
        )
        client = APIClient()
        client.force_authenticate(user=self.bob)
        resp = client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.post.id, self._ids(resp))

    def test_staff_sees_every_post_without_relation(self):
        staff = User.objects.create_user(
            username="staff", password="p", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = client.get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.post.id, self._ids(resp))


class JobPostDiscoveryCreateTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="p")
        self.bob = User.objects.create_user(username="bob", password="p")
        self.company = Company.objects.create(name="Acme")

    def _post(self, user, link, title="Engineer"):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(
            "/api/v1/job-posts/",
            {
                "data": {
                    "type": "job-post",
                    "attributes": {
                        "title": title,
                        "link": link,
                        "description": "x" * 500,
                    },
                }
            },
            format="json",
        )

    def test_fresh_create_records_discovery(self):
        resp = self._post(self.alice, "https://acme.example/jobs/a")
        self.assertEqual(resp.status_code, 201)
        post_id = int(resp.json()["data"]["id"])
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post_id=post_id, user=self.alice
            ).exists()
        )

    def test_link_dedupe_records_discovery_for_caller(self):
        # Alice ingests first.
        first = self._post(self.alice, "https://acme.example/jobs/b")
        self.assertEqual(first.status_code, 201)
        post_id = int(first.json()["data"]["id"])

        # Bob POSTs the same link → 200 echo + Bob now has a discovery.
        second = self._post(self.bob, "https://acme.example/jobs/b")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(int(second.json()["data"]["id"]), post_id)
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post_id=post_id, user=self.bob
            ).exists()
        )

    def test_repeat_post_is_idempotent(self):
        link = "https://acme.example/jobs/c"
        self._post(self.alice, link)
        self._post(self.alice, link)  # second call should not duplicate
        self.assertEqual(
            JobPostDiscovery.objects.filter(user=self.alice).count(), 1
        )


class JobPostEmailIngestVisibilityTests(TestCase):
    """End-to-end coverage for cc_auto's email-ingest contract: every
    POST to /api/v1/job-posts/ — fresh, link-deduped, fingerprint-deduped
    — must produce a JobPostDiscovery for the caller AND the post must
    surface in that caller's subsequent /api/v1/job-posts/ list. The
    user-visible bug this guards against is "ran the email processor,
    cc_auto logged 6 created posts, but I don't see them in the UI".

    Auth class is intentionally NOT the variable here — `force_authenticate`
    pins request.user, mirroring whatever resolved auth would set. The
    behavior we're locking in is post→discovery→visible regardless of
    whether the human reaches the API via cc_auto's API key or via the
    frontend's JWT."""

    def setUp(self):
        self.dough = User.objects.create_user(username="dough", password="p")
        self.company = Company.objects.create(name="Acme")

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.dough)
        return client

    def _payload(self, link, title="Engineer", source="email", **extra):
        attrs = {
            "title": title,
            "link": link,
            "description": "x" * 500,
            "source": source,
            **extra,
        }
        return {"data": {"type": "job-post", "attributes": attrs}}

    def _list_ids(self, user=None):
        resp = self._client(user).get("/api/v1/job-posts/")
        self.assertEqual(resp.status_code, 200)
        return {int(r["id"]) for r in resp.json()["data"]}

    def test_fresh_post_lands_in_caller_list(self):
        """cc_auto fresh-create case: POST → 201 → discovery for poster
        → post visible in poster's list. The 6×201 path from prod logs."""
        resp = self._client().post(
            "/api/v1/job-posts/",
            self._payload("https://acme.example/jobs/email-fresh"),
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        post_id = int(resp.json()["data"]["id"])

        disc = JobPostDiscovery.objects.filter(
            job_post_id=post_id, user=self.dough
        ).first()
        self.assertIsNotNone(disc, "discovery row missing — list filter will hide post")
        self.assertEqual(disc.source, "email")
        self.assertIn(post_id, self._list_ids())

    def test_link_dedupe_post_lands_in_caller_list(self):
        """cc_auto's most common case: a JobPost with this link already
        exists (created earlier by a scrape/paste/seed). POST returns
        200 echoing the existing record. Discovery for the *current*
        caller still must land — that's the only signal the user has
        on this shared post.

        Reproduces the Microsoft-200-OK row from the email-pipeline log:
        existing post + new caller → 200 + new discovery → visible."""
        link = "https://acme.example/jobs/email-link-dedupe"
        seeder = User.objects.create_user(username="seeder", password="p")
        existing = JobPost.objects.create(
            title="SRE",
            company=self.company,
            link=link,
            description="y" * 500,
            created_by=seeder,
        )
        # Sanity: dough has zero signal on this post → not visible yet.
        self.assertNotIn(existing.id, self._list_ids())

        resp = self._client().post(
            "/api/v1/job-posts/", self._payload(link), format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(int(resp.json()["data"]["id"]), existing.id)

        disc = JobPostDiscovery.objects.filter(
            job_post_id=existing.id, user=self.dough
        ).first()
        self.assertIsNotNone(
            disc,
            "link-dedupe branch must record discovery — without it, the "
            "post stays invisible to the caller even though POST 200'd",
        )
        self.assertEqual(disc.source, "email")
        self.assertIn(existing.id, self._list_ids())

    def test_fingerprint_dedupe_post_lands_in_caller_list(self):
        """No link match (or different URL), but title+company+location
        fingerprint matches an existing post within the 30-day window.
        POST returns 200 echoing the existing record + a discovery row."""
        seeder = User.objects.create_user(username="seeder", password="p")
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            link="https://acme.example/jobs/email-fp-orig",
            description="z" * 500,
            location="Remote",
            created_by=seeder,
        )

        resp = self._client().post(
            "/api/v1/job-posts/",
            {
                "data": {
                    "type": "job-post",
                    "attributes": {
                        "title": "Senior Engineer",
                        # Different link → forces the fingerprint branch.
                        "link": "https://acme.example/jobs/email-fp-other",
                        "description": "x" * 500,
                        "location": "Remote",
                        "source": "email",
                    },
                    "relationships": {
                        "company": {
                            "data": {"type": "company", "id": str(self.company.id)}
                        }
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(int(resp.json()["data"]["id"]), existing.id)
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post_id=existing.id, user=self.dough, source="email"
            ).exists()
        )
        self.assertIn(existing.id, self._list_ids())

    def test_six_email_batch_all_visible(self):
        """Reproduces the exact shape of the email-pipeline log the
        user shared: 1 link-dedupe (200) + 5 fresh (201). All 6 posts
        must be present in dough's list view after the batch — that's
        the user-visible promise."""
        # Pre-seed Microsoft so the first POST takes the link-dedupe branch.
        seeder = User.objects.create_user(username="seeder", password="p")
        ms = JobPost.objects.create(
            title="Security Engineer",
            company=self.company,
            link="https://www.linkedin.com/comm/jobs/view/4407832996/",
            description="m" * 500,
            created_by=seeder,
        )

        batch = [
            ("Security Engineer", "https://www.linkedin.com/comm/jobs/view/4407832996/", 200),
            ("Senior Cybersecurity Engineer", "https://www.linkedin.com/comm/jobs/view/4407897272/", 201),
            ("Security Engineer", "https://www.linkedin.com/comm/jobs/view/4394453711/", 201),
            ("Sr. Consultant SW Engineer", "https://www.linkedin.com/comm/jobs/view/4407892354/", 201),
            ("Senior Software Engineer, AI Platform", "https://www.linkedin.com/comm/jobs/view/4385659032/", 201),
            ("Agentic Developer", "https://www.linkedin.com/comm/jobs/view/4406562179/", 201),
        ]

        post_ids = set()
        for title, link, expected_status in batch:
            resp = self._client().post(
                "/api/v1/job-posts/",
                self._payload(link, title=title),
                format="json",
            )
            self.assertEqual(
                resp.status_code, expected_status,
                f"{title} ({link}) → {resp.status_code}: {resp.content[:200]}"
            )
            post_ids.add(int(resp.json()["data"]["id"]))

        self.assertEqual(len(post_ids), 6, "expected 6 distinct posts")
        self.assertIn(ms.id, post_ids, "link-dedupe should echo existing id")

        # Every one of the 6 has a discovery row tying it to dough.
        disc_count = JobPostDiscovery.objects.filter(
            user=self.dough, job_post_id__in=post_ids
        ).count()
        self.assertEqual(
            disc_count, 6,
            "discovery missing for some of the 6 posts — those rows will be "
            "invisible in the caller's list",
        )

        # And dough's list view returns all 6.
        visible_ids = self._list_ids()
        missing = post_ids - visible_ids
        self.assertFalse(
            missing,
            f"posts created by dough are missing from dough's list: {missing}",
        )

    def test_link_dedupe_fills_empty_company_on_existing_post(self):
        """Reproduces user-reported prod bug: cc_auto's email pipeline
        POSTed Microsoft job link 4407832996 with company_id=78, got
        200 (link-dedupe), but `select * from job_post where company_id=78`
        still showed only the prior two rows. Today's post isn't on the
        Microsoft company page because the dedupe path discards the
        incoming company association.

        Contract: when the existing post has NULL company_id and the
        incoming POST carries one, dedupe should fill it. Otherwise
        cc_auto can never recover company linkage on a post that was
        seeded without one (e.g. via an earlier scrape that found the
        URL before the company was known)."""
        link = "https://www.linkedin.com/comm/jobs/view/4407832996/"
        seeder = User.objects.create_user(username="seeder", password="p")
        existing = JobPost.objects.create(
            title="Security Engineer",
            company=None,  # the seed case: link known, company unknown
            link=link,
            description="m" * 500,
            created_by=seeder,
        )
        self.assertIsNone(existing.company_id)

        resp = self._client().post(
            "/api/v1/job-posts/",
            {
                "data": {
                    "type": "job-post",
                    "attributes": {
                        "title": "Security Engineer",
                        "link": link,
                        "description": "x" * 500,
                        "source": "email",
                    },
                    "relationships": {
                        "company": {
                            "data": {"type": "company", "id": str(self.company.id)}
                        }
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        existing.refresh_from_db()
        self.assertEqual(
            existing.company_id, self.company.id,
            "link-dedupe must fill empty company_id from incoming POST — "
            "without it the post never appears on /companies/<id>/job-posts",
        )

    def test_link_dedupe_does_not_overwrite_existing_company(self):
        """Companion to the fill-empty test: when the existing post
        already has a company, dedupe must NOT silently re-point it.
        The cc_auto pipeline guesses the company from the email subject
        and can be wrong; we don't want a wrong guess to clobber a
        good association.

        If we ever want a "force update" path it should be explicit
        (e.g. PATCH), not a side effect of POSTing a link that happens
        to already exist."""
        link = "https://acme.example/jobs/already-companied"
        other_company = Company.objects.create(name="Other Co")
        existing = JobPost.objects.create(
            title="SRE",
            company=other_company,
            link=link,
            description="m" * 500,
            created_by=User.objects.create_user(username="seeder", password="p"),
        )

        resp = self._client().post(
            "/api/v1/job-posts/",
            {
                "data": {
                    "type": "job-post",
                    "attributes": {
                        "title": "SRE",
                        "link": link,
                        "description": "x" * 500,
                        "source": "email",
                    },
                    "relationships": {
                        "company": {
                            "data": {"type": "company", "id": str(self.company.id)}
                        }
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        existing.refresh_from_db()
        self.assertEqual(existing.company_id, other_company.id)

    def test_company_job_posts_surfaces_post_via_discovery(self):
        """Reproduces the second half of the user-reported bug: even
        once the JobPost is correctly associated with company 78, the
        `/api/v1/companies/78/job-posts` endpoint must return it for
        the discovery-only caller. The whole universal-job-post refactor
        is moot if this list still scopes to created_by/applied/scored.

        Without discovery in the filter clause, dough has to manually
        score or apply to a post just to see it on a company page —
        which is exactly the toil the discovery row was meant to remove."""
        # Post on company; dough's only signal is a discovery row.
        post = JobPost.objects.create(
            title="Security Engineer",
            company=self.company,
            link="https://www.linkedin.com/comm/jobs/view/4407832996/",
            description="m" * 500,
            created_by=User.objects.create_user(username="seeder", password="p"),
        )
        JobPostDiscovery.objects.create(
            job_post=post, user=self.dough, source="email"
        )

        resp = self._client().get(f"/api/v1/companies/{self.company.id}/job-posts/")
        self.assertEqual(resp.status_code, 200)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        self.assertIn(
            post.id, ids,
            "discovery should surface a company's job-post on the company "
            "page; otherwise email-ingested posts stay invisible there",
        )

    def test_company_job_posts_six_email_batch_visible_on_company_page(self):
        """End-to-end replay of today's email batch + visit to the
        company page. After cc_auto POSTs (mix of fresh + link-dedupe),
        every post tied to Microsoft (company 78 in prod) must show on
        /companies/<id>/job-posts. Combines the two fixes (link-dedupe
        merging company + company endpoint honoring discovery)."""
        # Pre-seed Microsoft job (link-known, company-unknown — the prod
        # state cc_auto encounters when an earlier scrape created a
        # stub).
        seeded = JobPost.objects.create(
            title="Security Engineer",
            company=None,
            link="https://www.linkedin.com/comm/jobs/view/4407832996/",
            description="m" * 500,
            created_by=User.objects.create_user(username="seeder", password="p"),
        )

        # cc_auto POST: Microsoft → company_id should attach via dedupe.
        resp = self._client().post(
            "/api/v1/job-posts/",
            {
                "data": {
                    "type": "job-post",
                    "attributes": {
                        "title": "Security Engineer",
                        "link": "https://www.linkedin.com/comm/jobs/view/4407832996/",
                        "description": "x" * 500,
                        "source": "email",
                    },
                    "relationships": {
                        "company": {
                            "data": {"type": "company", "id": str(self.company.id)}
                        }
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # Microsoft's company page must include the deduped post.
        company_page = self._client().get(
            f"/api/v1/companies/{self.company.id}/job-posts/"
        )
        ids = {int(r["id"]) for r in company_page.json()["data"]}
        self.assertIn(
            seeded.id, ids,
            "user reported: 'do you see any job posts from today from "
            "Microsoft?' — no. This test fails until link-dedupe fills "
            "company_id AND the company endpoint honors discovery.",
        )

    def test_caller_only_sees_own_discoveries(self):
        """Tenant-isolation guard: posts another user POSTed (and the
        discoveries that go with them) must NOT leak into dough's list.
        Pairs with the link-dedupe test — the same shared post becomes
        visible only when dough's own discovery row exists."""
        eve = User.objects.create_user(username="eve", password="p")
        link = "https://acme.example/jobs/email-isolation"

        eve_resp = self._client(eve).post(
            "/api/v1/job-posts/", self._payload(link), format="json"
        )
        self.assertEqual(eve_resp.status_code, 201)
        eve_post_id = int(eve_resp.json()["data"]["id"])

        self.assertNotIn(
            eve_post_id, self._list_ids(),
            "dough has no signal on this post — it must stay hidden",
        )

        # Now dough re-POSTs the same link → link-dedupe → discovery → visible.
        dough_resp = self._client().post(
            "/api/v1/job-posts/", self._payload(link), format="json"
        )
        self.assertEqual(dough_resp.status_code, 200)
        self.assertEqual(int(dough_resp.json()["data"]["id"]), eve_post_id)
        self.assertIn(eve_post_id, self._list_ids())


class JobPostRetrieveDiscoveryTests(TestCase):
    """Single-GET access check must mirror the list filter — discovery is
    sufficient. Without this, a user receives an email-ingested post in
    /job-posts but clicking through 404s."""

    def setUp(self):
        self.dough = User.objects.create_user(username="dough", password="p")
        self.seeder = User.objects.create_user(username="seeder", password="p")
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="SRE",
            company=self.company,
            link="https://acme.example/jobs/retrieve",
            description="x" * 500,
            created_by=self.seeder,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_retrieve_404s_without_any_signal(self):
        resp = self._client(self.dough).get(f"/api/v1/job-posts/{self.post.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_retrieve_surfaces_post_via_discovery(self):
        JobPostDiscovery.objects.create(
            job_post=self.post, user=self.dough, source="email"
        )
        resp = self._client(self.dough).get(f"/api/v1/job-posts/{self.post.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(int(resp.json()["data"]["id"]), self.post.id)

    def test_retrieve_surfaces_post_for_staff(self):
        staff = User.objects.create_user(username="staff", password="p", is_staff=True)
        resp = self._client(staff).get(f"/api/v1/job-posts/{self.post.id}/")
        self.assertEqual(resp.status_code, 200)


class CompanyJobPostsRelationshipTests(TestCase):
    """The CompanySerializer's `job-posts` relationship (sideloaded via
    `?include=job-posts` on a company GET) must agree with the
    `/companies/<id>/job-posts/` list endpoint. Without matching filters
    the frontend sees inconsistent counts on the same page."""

    def setUp(self):
        self.dough = User.objects.create_user(username="dough", password="p")
        self.seeder = User.objects.create_user(username="seeder", password="p")
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="Security Engineer",
            company=self.company,
            link="https://acme.example/jobs/sideload",
            description="x" * 500,
            created_by=self.seeder,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _included_job_post_ids(self, resp):
        body = resp.json()
        return {
            int(item["id"])
            for item in body.get("included", [])
            if item.get("type") == "job-post"
        }

    def test_sideload_excludes_post_without_signal(self):
        resp = self._client(self.dough).get(
            f"/api/v1/companies/{self.company.id}/?include=job-posts"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.post.id, self._included_job_post_ids(resp))

    def test_sideload_includes_post_via_discovery(self):
        JobPostDiscovery.objects.create(
            job_post=self.post, user=self.dough, source="email"
        )
        resp = self._client(self.dough).get(
            f"/api/v1/companies/{self.company.id}/?include=job-posts"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            self.post.id, self._included_job_post_ids(resp),
            "sideloaded company.job-posts must honor discovery — "
            "otherwise it disagrees with /companies/<id>/job-posts/",
        )

    def test_sideload_staff_sees_every_post(self):
        staff = User.objects.create_user(username="staff", password="p", is_staff=True)
        resp = self._client(staff).get(
            f"/api/v1/companies/{self.company.id}/?include=job-posts"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.post.id, self._included_job_post_ids(resp))


class ReportsScopedJobPostsTests(TestCase):
    """`reports._user_scoped_job_posts` is the helper every report
    endpoint uses to scope to the caller. Must include discovery so
    email-ingested posts show up in source/sankey/funnel analytics."""

    def test_scoped_helper_includes_posts_via_discovery(self):
        from job_hunting.api.views.reports import _user_scoped_job_posts

        dough = User.objects.create_user(username="dough", password="p")
        seeder = User.objects.create_user(username="seeder", password="p")
        company = Company.objects.create(name="Acme")
        post = JobPost.objects.create(
            title="Engineer",
            company=company,
            link="https://acme.example/jobs/reports",
            description="x" * 500,
            created_by=seeder,
        )
        JobPostDiscovery.objects.create(job_post=post, user=dough, source="email")

        ids = set(_user_scoped_job_posts(dough.id).values_list("id", flat=True))
        self.assertIn(
            post.id, ids,
            "report helper must include discovery — undercounts per-user "
            "analytics otherwise",
        )
