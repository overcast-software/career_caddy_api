"""BACK-129 — report totals read the canonical visibility predicate.

Follow-up to BACK-128, which moved the job-post visibility filter to one
home on `JobPostQuerySet.visible_to`. `reports._user_scoped_job_posts` kept
its own inlined copy, and that copy had five clauses where the canonical
predicate has six:

    created · applied · scored · scraped · discovered · member

The missing one was `member` (`Q(user_memberships__user_id=...)`), so every
report UNDERCOUNTED posts arriving through the multi-user forward@ ingest
path. The same post was counted on the job-posts list and the company page
but not in report totals — a row present in some surfaces and absent from
others, which is worse than being wrong everywhere.

`_user_scoped_job_posts` now delegates. What is asserted here is both halves
of that delegation:

1. The member clause reaches report totals (the regression).
2. The other five still count exactly what they counted before — a
   delegation that changed the other clauses would be a different bug
   wearing this fix's clothes.
3. Reports do NOT inherit `visible_to`'s staff escape. That is a deliberate
   ruling, not an omission: reports already have an explicit everyone's-data
   door in `?scope=all`, and `?user=<id>` is documented as scoping TO THAT
   PERSON. Had the helper been pointed at `visible_to(user)`, `scope=mine`
   would silently mean "the entire platform" for an admin and `?user=<a
   staff member>` would return everyone's posts instead of that person's.
   Hence the id-based, staff-free entry point `visible_to_user_id`, which
   is where the clause list itself now lives.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.api.views.reports import _user_scoped_job_posts
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


class _ReportsBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.dough = User.objects.create_user(username="dough", password="pw")
        # Authored by someone else on purpose: every post below is reached by
        # a per-user SIGNAL, never by `created_by == me`. JobPost rows are
        # universal; authorship is not ownership.
        self.seeder = User.objects.create_user(username="seeder", password="pw")

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def _post(self, title):
        return JobPost.objects.create(
            title=title, company=self.company, created_by=self.seeder,
        )

    def _flow_total(self, client, query=""):
        resp = client.get(f"/api/v1/reports/application-flow/{query}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.json()["data"]["attributes"]["total_job_posts"]

    def _sources_total(self, client, query=""):
        resp = client.get(f"/api/v1/reports/sources/{query}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.json()["data"]["attributes"]["total_job_posts"]

    def _scoped_ids(self, user_id):
        return set(_user_scoped_job_posts(user_id).values_list("id", flat=True))


class TestMemberVisiblePostsReachReportTotals(_ReportsBase):
    """The regression. A post Dough can see ONLY through `UserJobPost` —
    the multi-user forward@ ownership join — was invisible to every report
    while the helper carried five clauses."""

    def setUp(self):
        super().setUp()
        self.jp_member = self._post("Member Post")
        UserJobPost.objects.create(job_post=self.jp_member, user=self.dough)

    def test_helper_includes_member_only_post(self):
        self.assertIn(
            self.jp_member.id, self._scoped_ids(self.dough.id),
            "the member clause is part of the canonical predicate — a report "
            "helper missing it undercounts every forward@-ingested post",
        )

    def test_application_flow_total_counts_member_only_post(self):
        self.assertEqual(self._flow_total(self._client(self.dough)), 1)

    def test_sources_total_counts_member_only_post(self):
        self.assertEqual(self._sources_total(self._client(self.dough)), 1)

    def test_member_post_stays_invisible_to_an_unrelated_user(self):
        """Delegation must not have widened the predicate. A user with NO
        signal on the row still sees nothing."""
        stranger = User.objects.create_user(username="stranger", password="pw")
        self.assertEqual(self._scoped_ids(stranger.id), set())
        self.assertEqual(self._flow_total(self._client(stranger)), 0)


class TestOtherFiveSignalsUnchanged(_ReportsBase):
    """Totals for the five pre-existing clauses are exactly what they were.
    One post per signal, each reachable by that signal alone."""

    def setUp(self):
        super().setUp()
        self.jp_created = JobPost.objects.create(
            title="Created", company=self.company, created_by=self.dough,
        )
        self.jp_applied = self._post("Applied")
        JobApplication.objects.create(
            user=self.dough, job_post=self.jp_applied, company=self.company,
            status="applied",
        )
        self.jp_scored = self._post("Scored")
        Score.objects.create(job_post=self.jp_scored, user=self.dough, score=42)
        self.jp_scraped = self._post("Scraped")
        Scrape.objects.create(
            url="https://acme.test/s", job_post=self.jp_scraped,
            company=self.company, created_by=self.dough,
        )
        self.jp_discovered = self._post("Discovered")
        JobPostDiscovery.objects.create(
            job_post=self.jp_discovered, user=self.dough, source="email",
        )
        # The leak canary: no signal for Dough at all.
        self.jp_stranger = self._post("Stranger Post")

    def test_all_five_signals_still_scope_in(self):
        self.assertEqual(
            self._scoped_ids(self.dough.id),
            {
                self.jp_created.id, self.jp_applied.id, self.jp_scored.id,
                self.jp_scraped.id, self.jp_discovered.id,
            },
        )

    def test_totals_count_five_and_exclude_the_unsignalled_post(self):
        client = self._client(self.dough)
        self.assertEqual(self._flow_total(client), 5)
        self.assertEqual(self._sources_total(client), 5)

    def test_a_post_carrying_two_signals_is_counted_once(self):
        """`.distinct()` survives the delegation — the predicate is a join
        across five reverse FKs, so a doubly-signalled row would otherwise
        inflate the total."""
        JobPostDiscovery.objects.create(
            job_post=self.jp_created, user=self.dough, source="email",
        )
        UserJobPost.objects.create(job_post=self.jp_created, user=self.dough)
        self.assertEqual(self._flow_total(self._client(self.dough)), 5)


class TestReportsDoNotInheritTheStaffEscape(_ReportsBase):
    """The deliberate ruling. `visible_to` hands staff the whole table;
    `_user_scoped_job_posts` must not, because reports gate cross-user
    reads at `?scope=all` and would otherwise carry a second, ungated door.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="admin129", password="pw", is_staff=True,
        )
        self.jp_admin = self._post("Admin's Own")
        UserJobPost.objects.create(job_post=self.jp_admin, user=self.admin)
        self.jp_dough = self._post("Dough's Own")
        UserJobPost.objects.create(job_post=self.jp_dough, user=self.dough)

    def test_helper_is_person_scoped_even_for_staff(self):
        self.assertEqual(self._scoped_ids(self.admin.id), {self.jp_admin.id})

    def test_scope_mine_means_mine_for_staff_too(self):
        """If reports had inherited the staff escape, an admin's `scope=mine`
        would quietly report the entire platform and be indistinguishable
        from `scope=all`."""
        self.assertEqual(
            self._flow_total(self._client(self.admin), "?scope=mine"), 1
        )
        self.assertEqual(
            self._sources_total(self._client(self.admin), "?scope=mine"), 1
        )

    def test_person_filter_scopes_to_that_person_not_the_whole_table(self):
        """`?user=<id>` is documented as scoping TO THAT PERSON. Under the
        staff escape it would depend on whether the TARGET is staff — the
        admin here asks for Dough and must get Dough's one post."""
        self.assertEqual(
            self._flow_total(
                self._client(self.admin), f"?scope=all&user={self.dough.id}"
            ),
            1,
        )

    def test_person_filter_targeting_staff_returns_only_that_persons_posts(self):
        """The case the staff escape would break hardest: the person being
        scoped TO is themselves staff. `visible_to(target)` would return
        every post on the platform under the guise of a per-person filter."""
        self.assertEqual(
            self._flow_total(
                self._client(self.admin), f"?scope=all&user={self.admin.id}"
            ),
            1,
        )

    def test_scope_all_remains_the_one_cross_user_door(self):
        """Staff still see everything — through the explicit, gated door."""
        self.assertEqual(
            self._flow_total(self._client(self.admin), "?scope=all"), 2
        )

    def test_scope_all_is_still_refused_to_non_staff(self):
        resp = self._client(self.dough).get(
            "/api/v1/reports/application-flow/?scope=all"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class TestNullUserIdIsEmptyNotUnfiltered(_ReportsBase):
    """A falsy user id must yield nothing, never the unfiltered table.
    `Q(created_by_id=None)` compiles to `created_by_id IS NULL` and would
    match every ownerless post — the exact footgun the canonical predicate
    short-circuits. The anonymous demo path reaches this helper with an id
    resolved from a `guest` user that need not exist on every instance.
    """

    def test_none_user_id_scopes_to_nothing(self):
        JobPost.objects.create(title="Ownerless", company=self.company)
        self.assertEqual(self._scoped_ids(None), set())
