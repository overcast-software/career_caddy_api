"""BACK-128 — Company's `job-posts` / `job-applications` data linkage.

Two things are asserted here, and they pull in different directions:

1. **The linkage is emitted for the includes the frontend actually sends.**
   A to-many emits `data` only when the caller requested it via `?include=`
   — that pairing is deliberate, because linkage WITHOUT the records in
   `included` makes Ember Data fetch them one GET per record (the legacy
   adapter's `coalesceFindRequests` defaults to false). The bug was that
   `_requested_includes` matched the whole undivided include string, so a
   DOTTED path never matched: `?include=job-applications.job-post` — the
   literal include on the questions form — sideloaded its records but
   emitted no linkage for the `job-applications` hop, and Ember Data
   refetched through `links.related`, one request per company.

2. **The linkage is user-scoped, on the six-clause filter.** `Company` is
   shared across all users (no `created_by` on the model), so its
   `job_posts` reverse FK spans the whole platform. An unscoped linkage
   publishes the ids and existence of every other user's posts at that
   company. Under-scoping is equally broken: an embedded id the caller
   cannot fetch makes Ember Data 404 and silently drop it.

Visibility is NOT `created_by == me`. It is the six-clause filter now
living on `JobPostQuerySet.visible_to`:

    created · applied · scored · scraped · discovered · member

Before this change the serializer and `/companies/<id>/job-posts/` each
carried their own inlined copy of the predicate, and both had lost the
`member` clause that `JobPostViewSet._visible_jobpost_qs` still had — the
drift that motivated moving it to one home on the model layer. (The
serializer cannot import from `views`; that dependency is one-way.)
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.api.serializers import CompanySerializer
from job_hunting.api.views.jobs import JobPostViewSet
from job_hunting.models import (
    Company,
    JobApplication,
    JobPost,
    JobPostDiscovery,
    Score,
    Scrape,
    UserJobPost,
)

User = get_user_model()

# The literal include the questions form sends (components/questions/
# form.js `_preloadCompanyRelated`). Its `job-applications` hop is dotted,
# which is precisely what used to fall through the linkage gate.
FRONTEND_INCLUDE = "job-posts,job-applications.job-post"


class _SharedCompanyBase(TestCase):
    """One shared Company. Alice and Bob each have exactly one post they
    can see, reached by a DIFFERENT visibility clause per test class, plus
    one post belonging to a third user that neither may ever see."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="pw")
        self.bob = User.objects.create_user(username="bob", password="pw")
        self.carol = User.objects.create_user(username="carol", password="pw")
        self.company = Company.objects.create(name="Acme")

        self.jp_alice = JobPost.objects.create(
            title="Alice Post", company=self.company, created_by=self.alice,
        )
        self.jp_bob = JobPost.objects.create(
            title="Bob Post", company=self.company, created_by=self.bob,
        )
        # Carol's post — the leak canary. Neither Alice nor Bob has any
        # signal on it, so it must never appear in either linkage.
        self.jp_carol = JobPost.objects.create(
            title="Carol Post", company=self.company, created_by=self.carol,
        )

        self.client_alice = APIClient()
        self.client_alice.force_authenticate(user=self.alice)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.bob)

    def _get_company(self, client, include="job-posts"):
        url = f"/api/v1/companies/{self.company.id}/"
        if include:
            url = f"{url}?include={include}"
        return client.get(url)

    def _company_resource(self, resp, company_id=None):
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        target = str(company_id if company_id is not None else self.company.id)
        data = body.get("data")
        if isinstance(data, dict):
            return data
        for chunk in data or []:
            if chunk.get("type") == "company" and str(chunk.get("id")) == target:
                return chunk
        return None

    def _linkage_ids(self, resource, rel_name):
        """The set of ids in `relationships.<rel>.data`. Fails the test when
        the key is absent — links-only IS the defect, so it must never be
        mistaken for an empty linkage."""
        rels = resource.get("relationships") or {}
        rel = rels.get(rel_name)
        self.assertIsNotNone(rel, f"No `{rel_name}` relationship on the resource")
        self.assertIn(
            "data", rel,
            f"`{rel_name}` emitted links-only — Ember Data cannot resolve the "
            f"hasMany from `included` and will refetch via links.related",
        )
        return {entry["id"] for entry in rel["data"]}

    def _sideloaded_ids(self, resp, rtype):
        return {
            c["id"] for c in (resp.json().get("included") or [])
            if c["type"] == rtype
        }


class TestDottedIncludeEmitsLinkage(_SharedCompanyBase):
    """A dotted include path must emit `data` linkage for its FIRST hop.
    Matching the whole undivided string meant `job-applications.job-post`
    sideloaded records with no linkage to attach them to."""

    def setUp(self):
        super().setUp()
        self.app_alice = JobApplication.objects.create(
            user=self.alice, job_post=self.jp_alice, company=self.company,
            status="applied",
        )

    def test_plain_include_emits_job_applications_linkage(self):
        resp = self._get_company(self.client_alice, include="job-applications")
        ids = self._linkage_ids(
            self._company_resource(resp), "job-applications",
        )
        self.assertEqual(ids, {self.app_alice.id})

    def test_dotted_include_emits_job_applications_linkage(self):
        """`job-applications.job-post` requests this company's
        job-applications, plus a further hop. The first hop gets linkage."""
        resp = self._get_company(
            self.client_alice, include="job-applications.job-post",
        )
        ids = self._linkage_ids(
            self._company_resource(resp), "job-applications",
        )
        self.assertEqual(ids, {self.app_alice.id})

    def test_frontend_include_string_emits_both_linkages(self):
        """The exact include the questions form sends. Both hops must carry
        linkage, and both sets must match what landed in `included`."""
        resp = self._get_company(self.client_alice, include=FRONTEND_INCLUDE)
        resource = self._company_resource(resp)
        post_ids = self._linkage_ids(resource, "job-posts")
        app_ids = self._linkage_ids(resource, "job-applications")
        self.assertEqual(post_ids, {self.jp_alice.id})
        self.assertEqual(app_ids, {self.app_alice.id})
        self.assertEqual(post_ids, self._sideloaded_ids(resp, "job-post"))
        self.assertEqual(
            app_ids, self._sideloaded_ids(resp, "job-application"),
            "Every id in the linkage must be in `included`, or Ember Data "
            "fetches the missing ones one GET at a time",
        )

    def test_underscored_dotted_include_also_matches(self):
        """Tolerance for the underscored spelling survives the split."""
        resp = self._get_company(
            self.client_alice, include="job_applications.job_post",
        )
        ids = self._linkage_ids(
            self._company_resource(resp), "job-applications",
        )
        self.assertEqual(ids, {self.app_alice.id})

    def test_no_include_stays_links_only(self):
        """The gate still holds: without `?include=`, no `data`. Emitting
        linkage here would hand Ember Data ids with no loaded records and
        it would fetch them one at a time — worse than the single
        related-link request it makes today."""
        resp = self._get_company(self.client_alice, include=None)
        rels = self._company_resource(resp).get("relationships") or {}
        self.assertNotIn("data", rels.get("job-posts") or {})
        self.assertNotIn("data", rels.get("job-applications") or {})


class TestJobPostsLinkageIsUserScoped(_SharedCompanyBase):
    """Two users, same shared company: each sees only their own posts."""

    def test_alice_linkage_excludes_bobs_and_carols_posts(self):
        resp = self._get_company(self.client_alice)
        ids = self._linkage_ids(self._company_resource(resp), "job-posts")
        self.assertIn(self.jp_alice.id, ids)
        self.assertNotIn(self.jp_bob.id, ids)
        self.assertNotIn(self.jp_carol.id, ids)

    def test_bob_linkage_excludes_alices_and_carols_posts(self):
        resp = self._get_company(self.client_bob)
        ids = self._linkage_ids(self._company_resource(resp), "job-posts")
        self.assertIn(self.jp_bob.id, ids)
        self.assertNotIn(self.jp_alice.id, ids)
        self.assertNotIn(self.jp_carol.id, ids)

    def test_sideload_carries_only_the_callers_posts(self):
        """The leak has two surfaces — the linkage and `included`. Both
        must be scoped, or the ids leak through the sideloaded records."""
        resp = self._get_company(self.client_bob)
        self.assertEqual(
            self._sideloaded_ids(resp, "job-post"), {self.jp_bob.id},
        )

    def test_staff_linkage_sees_every_post_on_the_company(self):
        staff = User.objects.create_user(username="root", password="pw", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = self._get_company(client)
        ids = self._linkage_ids(self._company_resource(resp), "job-posts")
        self.assertEqual(
            ids, {self.jp_alice.id, self.jp_bob.id, self.jp_carol.id},
        )

    def test_list_endpoint_linkage_is_scoped_too(self):
        resp = self.client_bob.get("/api/v1/companies/?include=job-posts")
        ids = self._linkage_ids(self._company_resource(resp), "job-posts")
        self.assertEqual(ids, {self.jp_bob.id})


class TestJobPostsLinkageHonorsEveryVisibilityClause(_SharedCompanyBase):
    """Visibility is the six-clause filter, NOT `created_by == me`. Each
    test grants Bob a single non-ownership signal on Alice's post and
    asserts it surfaces — one test per clause, so a regression names the
    clause it broke."""

    def _bob_linkage(self):
        resp = self._get_company(self.client_bob)
        return self._linkage_ids(self._company_resource(resp), "job-posts")

    def test_applied_clause(self):
        JobApplication.objects.create(
            user=self.bob, job_post=self.jp_alice, company=self.company,
            status="applied",
        )
        self.assertIn(self.jp_alice.id, self._bob_linkage())

    def test_scored_clause(self):
        Score.objects.create(job_post=self.jp_alice, user=self.bob, score=50)
        self.assertIn(self.jp_alice.id, self._bob_linkage())

    def test_scraped_clause(self):
        Scrape.objects.create(
            url="https://acme.test/a", job_post=self.jp_alice,
            company=self.company, created_by=self.bob,
        )
        self.assertIn(self.jp_alice.id, self._bob_linkage())

    def test_discovered_clause(self):
        JobPostDiscovery.objects.create(job_post=self.jp_alice, user=self.bob)
        self.assertIn(self.jp_alice.id, self._bob_linkage())

    def test_member_clause(self):
        """The clause both inlined copies had lost. A post Bob owns via
        UserJobPost (the multi-user forward@ ownership join) but did not
        create was absent from the linkage before BACK-128, while
        JobPostViewSet showed it — the two disagreed."""
        UserJobPost.objects.create(job_post=self.jp_alice, user=self.bob)
        self.assertIn(self.jp_alice.id, self._bob_linkage())

    def test_member_clause_on_the_sub_collection_endpoint(self):
        """Same drift, second surface: `/companies/<id>/job-posts/` had its
        own copy of the predicate and had also lost `member`."""
        UserJobPost.objects.create(job_post=self.jp_alice, user=self.bob)
        resp = self.client_bob.get(
            f"/api/v1/companies/{self.company.id}/job-posts/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn(
            self.jp_alice.id, {r["id"] for r in resp.json()["data"]},
        )


class TestJobApplicationsLinkageIsUserScoped(_SharedCompanyBase):
    """`job-applications` is the same defect on the same serializer.
    JobApplication is per-user, so ownership IS the whole filter."""

    def setUp(self):
        super().setUp()
        self.app_alice = JobApplication.objects.create(
            user=self.alice, job_post=self.jp_alice, company=self.company,
            status="applied",
        )
        self.app_bob = JobApplication.objects.create(
            user=self.bob, job_post=self.jp_bob, company=self.company,
            status="applied",
        )

    def test_alice_sees_only_her_own_application(self):
        resp = self._get_company(self.client_alice, include="job-applications")
        ids = self._linkage_ids(self._company_resource(resp), "job-applications")
        self.assertEqual(ids, {self.app_alice.id})

    def test_bob_sees_only_his_own_application(self):
        resp = self._get_company(self.client_bob, include="job-applications")
        ids = self._linkage_ids(self._company_resource(resp), "job-applications")
        self.assertEqual(ids, {self.app_bob.id})

    def test_staff_does_not_bypass_per_user_applications(self):
        """Unlike job-posts, there is no staff bypass here — a JobApplication
        belongs to exactly one user and staff has none of their own."""
        staff = User.objects.create_user(username="root2", password="pw", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = self._get_company(client, include="job-applications")
        ids = self._linkage_ids(self._company_resource(resp), "job-applications")
        self.assertEqual(ids, set())


class TestGetRelatedWithNoUser(_SharedCompanyBase):
    """A serializer used outside a request (shell, fixtures, an internal
    caller) has no user. `get_related` must resolve to NO rows — never to
    an unfiltered queryset, which is the whole platform's posts. Before
    this change the `if user_id:` guard simply skipped the filter."""

    def setUp(self):
        super().setUp()
        JobApplication.objects.create(
            user=self.alice, job_post=self.jp_alice, company=self.company,
            status="applied",
        )

    def test_job_posts_with_no_request_is_empty(self):
        _rtype, items = CompanySerializer().get_related(self.company, "job-posts")
        self.assertEqual(
            items, [],
            "No request must mean no rows — an unfiltered result here would "
            "expose every user's posts at this company",
        )

    def test_job_applications_with_no_request_is_empty(self):
        _rtype, items = CompanySerializer().get_related(
            self.company, "job-applications",
        )
        self.assertEqual(items, [])

    def test_anonymous_request_is_rejected_at_the_endpoint(self):
        resp = APIClient().get(f"/api/v1/companies/{self.company.id}/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestLinkageAgreesWithSubCollectionEndpoint(_SharedCompanyBase):
    """`GET /companies/<id>/job-posts/` and the `job-posts` linkage must
    return the same set. A post in the linkage that the endpoint refuses is
    a 404 the frontend drops silently — the exact failure the serializer's
    scoping exists to prevent. Both now read `visible_to`."""

    def setUp(self):
        super().setUp()
        # Give Bob one post per non-ownership clause so the comparison
        # spans more than plain created_by.
        UserJobPost.objects.create(job_post=self.jp_alice, user=self.bob)
        JobPostDiscovery.objects.create(job_post=self.jp_carol, user=self.bob)

    def test_sets_match_for_a_regular_user(self):
        resp = self._get_company(self.client_bob)
        linkage = self._linkage_ids(self._company_resource(resp), "job-posts")
        sub = self.client_bob.get(f"/api/v1/companies/{self.company.id}/job-posts/")
        self.assertEqual(sub.status_code, status.HTTP_200_OK)
        endpoint = {r["id"] for r in sub.json()["data"]}
        self.assertEqual(linkage, endpoint)
        # And it is genuinely exercising the non-ownership clauses.
        self.assertEqual(
            linkage, {self.jp_bob.id, self.jp_alice.id, self.jp_carol.id},
        )

    def test_sets_match_for_staff(self):
        staff = User.objects.create_user(username="root3", password="pw", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = self._get_company(client)
        linkage = self._linkage_ids(self._company_resource(resp), "job-posts")
        sub = client.get(f"/api/v1/companies/{self.company.id}/job-posts/")
        endpoint = {r["id"] for r in sub.json()["data"]}
        self.assertEqual(linkage, endpoint)


class TestVisibleToRefactorGuard(_SharedCompanyBase):
    """`JobPostViewSet._visible_jobpost_qs` now delegates to
    `JobPost.objects.visible_to`. For any authenticated caller the two must
    resolve to the same set — that is the whole claim of the refactor, and
    all five of its call sites ride on it."""

    def setUp(self):
        super().setUp()
        # One post per clause, so the guard covers the full predicate
        # rather than just ownership.
        self.jp_applied = JobPost.objects.create(title="applied", company=self.company)
        JobApplication.objects.create(
            user=self.bob, job_post=self.jp_applied, company=self.company,
            status="applied",
        )
        self.jp_scored = JobPost.objects.create(title="scored", company=self.company)
        Score.objects.create(job_post=self.jp_scored, user=self.bob, score=10)
        self.jp_scraped = JobPost.objects.create(title="scraped", company=self.company)
        Scrape.objects.create(
            url="https://acme.test/s", job_post=self.jp_scraped,
            company=self.company, created_by=self.bob,
        )
        self.jp_discovered = JobPost.objects.create(title="disc", company=self.company)
        JobPostDiscovery.objects.create(job_post=self.jp_discovered, user=self.bob)
        self.jp_member = JobPost.objects.create(title="member", company=self.company)
        UserJobPost.objects.create(job_post=self.jp_member, user=self.bob)

    class _FakeRequest:
        def __init__(self, user):
            self.user = user

    def test_delegation_matches_for_a_regular_user(self):
        expected = {
            self.jp_bob.id, self.jp_applied.id, self.jp_scored.id,
            self.jp_scraped.id, self.jp_discovered.id, self.jp_member.id,
        }
        via_viewset = set(
            JobPostViewSet._visible_jobpost_qs(
                self._FakeRequest(self.bob)
            ).values_list("id", flat=True)
        )
        via_manager = set(
            JobPost.objects.visible_to(self.bob).values_list("id", flat=True)
        )
        self.assertEqual(via_viewset, via_manager)
        self.assertEqual(via_viewset, expected)
        # The canary: Carol's post reaches Bob through no clause at all.
        self.assertNotIn(self.jp_carol.id, via_viewset)

    def test_delegation_matches_for_staff(self):
        staff = User.objects.create_user(username="root4", password="pw", is_staff=True)
        via_viewset = set(
            JobPostViewSet._visible_jobpost_qs(
                self._FakeRequest(staff)
            ).values_list("id", flat=True)
        )
        via_manager = set(
            JobPost.objects.visible_to(staff).values_list("id", flat=True)
        )
        all_ids = set(JobPost.objects.values_list("id", flat=True))
        self.assertEqual(via_viewset, via_manager)
        self.assertEqual(via_viewset, all_ids)

    def test_visible_to_is_chainable_and_composes_with_a_company_filter(self):
        """The serializer composes it as
        `JobPost.objects.filter(company_id=...).visible_to(user)`; that has
        to narrow rather than reset."""
        other = Company.objects.create(name="Other")
        elsewhere = JobPost.objects.create(
            title="elsewhere", company=other, created_by=self.bob,
        )
        scoped = set(
            JobPost.objects.filter(company_id=self.company.id)
            .visible_to(self.bob)
            .values_list("id", flat=True)
        )
        self.assertIn(self.jp_bob.id, scoped)
        self.assertNotIn(elsewhere.id, scoped)

    def test_visible_to_none_user_is_empty_not_unfiltered(self):
        """`None` must short-circuit. Falling through to the Q-block would
        compile `created_by_id IS NULL` and match every ownerless post —
        the five created above among them."""
        self.assertEqual(list(JobPost.objects.visible_to(None)), [])
