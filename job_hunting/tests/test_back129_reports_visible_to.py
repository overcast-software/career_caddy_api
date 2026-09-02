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
   door in `?scope=all`, and the staff `?user=<id>` filter narrows THAT door
   to one person. Had the helper been pointed at `visible_to(user)`,
   `scope=mine` would silently mean "the entire platform" for an admin and
   `?scope=all&user=<a staff member>` would return everyone's posts instead
   of that person's. Hence the id-based, staff-free entry point
   `visible_to_user_id`, which is where the clause list itself now lives.

Note on what `?user=` does today: the views read it only on the `scope=all`
branch, so a staff `?user=42` under the default `scope=mine` returns the
CALLER's data. That gap pre-dates BACK-129 and is untouched here; it is why
every test below spells the person filter `?scope=all&user=<id>` rather than
`?user=<id>` alone. See `_user_scoped_job_posts` for the full note.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.api.views.reports import (
    PUBLIC_DEMO_FLOW_KEY,
    _user_scoped_job_posts,
)
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
        # Each post carries TWO signals: the member row and a Score. The
        # second one is what makes these assertions diagnostic. Built on the
        # member clause alone, every test here also went red when the member
        # clause was removed, so a failure could not tell "member clause
        # missing" apart from "staff escape inherited" — the two falsifications
        # this module exists to separate. With a non-member signal present the
        # posts stay visible under either predicate, and a red bar in this
        # class can only mean the staff escape leaked in.
        self.jp_admin = self._post("Admin's Own")
        UserJobPost.objects.create(job_post=self.jp_admin, user=self.admin)
        Score.objects.create(job_post=self.jp_admin, user=self.admin, score=11)
        self.jp_dough = self._post("Dough's Own")
        UserJobPost.objects.create(job_post=self.jp_dough, user=self.dough)
        Score.objects.create(job_post=self.jp_dough, user=self.dough, score=22)

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
        """The staff `?user=<id>` filter narrows `scope=all` to one person.
        Under the staff escape the result would depend on whether the TARGET
        is staff — the admin here asks for Dough and must get Dough's one
        post. (`?user=` is only read on the `scope=all` branch; see the note
        in `_user_scoped_job_posts` about the `scope=mine` gap.)"""
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
    short-circuits.

    The guard is defensive, and deliberately so; no current call site hands
    it `None`. The authenticated branches pass `request.user.id`, the person
    filter passes an int that `_person_filter_effective_user_id` already
    proved parseable, and the anonymous demo returns a zeroed payload at
    `reports.py` before calling the helper when no `guest` user exists. The
    one falsy value that does reach it from production is `?scope=all&user=0`
    — which lands on `.none()` rather than on `created_by_id = 0`, but would
    also be empty without the guard, so it is not asserted below. The
    assertion that bites is the `None` one: remove the short-circuit and the
    ownerless post appears.
    """

    def test_none_user_id_scopes_to_nothing(self):
        JobPost.objects.create(title="Ownerless", company=self.company)
        self.assertEqual(self._scoped_ids(None), set())


class TestAnonymousDemoWidensToo(_ReportsBase):
    """The one PUBLIC surface this fix moves.

    `application_flow_report` is AllowAny: unauthenticated callers get a
    cached payload derived from the `guest` user's pipeline, built through
    the same `_user_scoped_job_posts`. Gaining the member clause therefore
    changes what anonymous visitors see, so it is asserted rather than left
    to inference. The contract is unchanged — the scope is still "the guest
    user's signals", and the payload is aggregate counts, never row content.

    `_build_public_demo_sources` widened identically but has no caller
    anywhere in the repo, so there is no reachable behavior to pin.
    """

    def setUp(self):
        super().setUp()
        # LocMem cache is process-wide and outlives a TestCase, and this
        # endpoint memoizes its payload for PUBLIC_DEMO_CACHE_SECONDS.
        cache.clear()
        self.addCleanup(cache.clear)
        self.guest = User.objects.create_user(username="guest", password="pw")
        self.jp_guest_member = self._post("Guest Member Post")
        UserJobPost.objects.create(
            job_post=self.jp_guest_member, user=self.guest
        )

    def _anon_flow(self):
        resp = APIClient().get("/api/v1/reports/application-flow/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.json()["data"]["attributes"]

    def test_anonymous_demo_counts_guest_member_only_post(self):
        attrs = self._anon_flow()
        self.assertEqual(attrs["scope"], "public_demo")
        self.assertEqual(attrs["total_job_posts"], 1)

    def test_anonymous_demo_still_shows_only_the_guest_user(self):
        """The widening is by clause, not by scope. A post signalled for a
        real user stays out of the anonymous aggregate."""
        UserJobPost.objects.create(job_post=self._post("Dough's"), user=self.dough)
        self.assertEqual(self._anon_flow()["total_job_posts"], 1)

    def test_stale_v1_payload_is_not_served(self):
        """The cache key moved to :v2 because the payload's derivation
        changed. Left at :v1, every anonymous visitor would keep being served
        the pre-fix undercount for the rest of the TTL after deploy."""
        cache.set(
            "reports:public_demo:application_flow:v1",
            {
                "nodes": [], "links": [],
                "total_job_posts": 999, "total_applications": 999,
            },
            300,
        )
        self.assertEqual(self._anon_flow()["total_job_posts"], 1)

    def test_payload_is_cached_under_the_current_key(self):
        self.assertIsNone(cache.get(PUBLIC_DEMO_FLOW_KEY))
        self._anon_flow()
        self.assertEqual(cache.get(PUBLIC_DEMO_FLOW_KEY)["total_job_posts"], 1)
