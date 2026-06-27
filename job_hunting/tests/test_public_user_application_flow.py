"""CC-105 — public (AllowAny) application-flow (Sankey) endpoint tests.

Pins the contract of
``GET /api/v1/users/<username>/application-flow/``:

* public, no auth — anonymous client gets 200 (AllowAny)
* envelope is IDENTICAL to the authed ``application_flow_report``
  (``type: report``, ``id: application-flow``, attrs
  ``nodes``/``links``/``total_job_posts``/``total_applications``) plus
  ``scope: "public_profile"``
* gated on the owner's ``federate_rich`` opt-in — a rich owner with
  published posts gets a populated funnel
* federate_rich False, unknown username, or no published posts → an
  EMPTY flow with HTTP 200 (never 403/404)
* the funnel counts ONLY published (audience-public) posts — the same
  queryset that backs the public federated feed, never the full pipeline
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Profile,
    Status,
)
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()

URL = "/api/v1/users/{username}/application-flow/"

# Attribute keys the public report envelope must carry — identical to the
# authed application_flow_report payload, plus scope.
_EXPECTED_ATTR_KEYS = {
    "nodes",
    "links",
    "total_job_posts",
    "total_applications",
    "scope",
}


def _status(name: str) -> Status:
    return Status.objects.get_or_create(status=name)[0]


def _log(app: JobApplication, status_name: str, *, days_ago: int = 0):
    when = timezone.now() - timedelta(days=days_ago)
    return JobApplicationStatus.objects.create(
        application=app,
        status=_status(status_name),
        logged_at=when,
    )


def _edge(attrs, src_name: str, dst_name: str) -> int:
    ids = {n["id"]: i for i, n in enumerate(attrs["nodes"])}
    if src_name not in ids or dst_name not in ids:
        return 0
    total = 0
    for link in attrs["links"]:
        if link["source"] == ids[src_name] and link["target"] == ids[dst_name]:
            total += link["value"]
    return total


class TestPublicApplicationFlow(TestCase):
    def setUp(self):
        # Anonymous client — NO auth header; AllowAny must serve it.
        self.client = APIClient()
        self.company = Company.objects.create(name="Bushwood CC")

        # Rich owner: opted into federate_rich, with one PUBLISHED post the
        # owner TRIAGED ("Vetted Good") and then advanced to Applied, so it
        # survives the CC-107 vetted-only filter and totals are non-zero,
        # plus a PRIVATE post that also has an application — it must never
        # count.
        self.rich = User.objects.create_user(username="dough", password="pass")
        Profile.objects.create(user=self.rich, federate_rich=True)

        self.published = JobPost.objects.create(
            created_by=self.rich,
            title="Senior Greenskeeper",
            description="Tend the greens",
            link="https://example.com/jobs/1",
            company=self.company,
            audience=[AS2_PUBLIC],
        )
        pub_app = JobApplication.objects.create(
            job_post=self.published, user=self.rich
        )
        # Vetted Good first, then Applied later: latest status is Applied,
        # but the existence of a vetting verdict keeps the post in the funnel.
        _log(pub_app, "Vetted Good", days_ago=6)
        _log(pub_app, "Applied", days_ago=5)

        # Private (audience=[]) — excluded from the published queryset, so
        # neither it nor its application may surface in the funnel totals.
        self.private = JobPost.objects.create(
            created_by=self.rich,
            title="Private musings",
            description="Not for the world",
            link="https://example.com/jobs/2",
            company=self.company,
            audience=[],
        )
        priv_app = JobApplication.objects.create(
            job_post=self.private, user=self.rich
        )
        _log(priv_app, "Applied", days_ago=5)

        # Lean owner: federate_rich False, but WITH a published+applied post.
        # The gate (not the data) is what suppresses the funnel.
        self.lean = User.objects.create_user(username="judge", password="pass")
        Profile.objects.create(user=self.lean, federate_rich=False)
        lean_pub = JobPost.objects.create(
            created_by=self.lean,
            title="Lean public role",
            description="Visible role",
            link="https://example.com/jobs/3",
            company=self.company,
            audience=[AS2_PUBLIC],
        )
        lean_app = JobApplication.objects.create(job_post=lean_pub, user=self.lean)
        _log(lean_app, "Applied", days_ago=5)

        # Rich owner with NOTHING published (only a private post).
        self.barerich = User.objects.create_user(
            username="spaulding", password="pass"
        )
        Profile.objects.create(user=self.barerich, federate_rich=True)
        JobPost.objects.create(
            created_by=self.barerich,
            title="Bare private",
            link="https://example.com/jobs/4",
            audience=[],
        )

    def _attrs(self, username: str):
        resp = self.client.get(URL.format(username=username))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["type"], "report")
        self.assertEqual(data["id"], "application-flow")
        attrs = data["attributes"]
        self.assertEqual(set(attrs.keys()), _EXPECTED_ATTR_KEYS)
        self.assertEqual(attrs["scope"], "public_profile")
        return attrs

    def test_rich_user_with_published_posts_populated_flow_allowany(self):
        # No auth header at all — AllowAny serves the anonymous visitor.
        attrs = self._attrs("dough")
        self.assertEqual(attrs["total_job_posts"], 1)
        self.assertEqual(attrs["total_applications"], 1)
        self.assertNotEqual(attrs["nodes"], [])
        self.assertNotEqual(attrs["links"], [])
        # The published post's Applied application flows through the funnel.
        self.assertEqual(_edge(attrs, "unscored", "applications"), 1)
        self.assertEqual(_edge(attrs, "applications", "applied"), 1)

    def test_rich_user_zero_published_posts_empty_flow_200(self):
        attrs = self._attrs("spaulding")
        self.assertEqual(attrs["nodes"], [])
        self.assertEqual(attrs["links"], [])
        self.assertEqual(attrs["total_job_posts"], 0)
        self.assertEqual(attrs["total_applications"], 0)

    def test_non_rich_user_with_published_posts_empty_flow_200(self):
        # The gate, not the data, suppresses the funnel: judge HAS a
        # published+applied post but federate_rich is False.
        attrs = self._attrs("judge")
        self.assertEqual(attrs["nodes"], [])
        self.assertEqual(attrs["links"], [])
        self.assertEqual(attrs["total_job_posts"], 0)
        self.assertEqual(attrs["total_applications"], 0)

    def test_unknown_username_empty_flow_200_not_404(self):
        resp = self.client.get(URL.format(username="nobody"))
        self.assertEqual(resp.status_code, 200)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["nodes"], [])
        self.assertEqual(attrs["total_job_posts"], 0)
        self.assertEqual(attrs["scope"], "public_profile")

    def test_funnel_counts_only_published_posts(self):
        # rich owner has 1 published + 1 private post, each with an Applied
        # application. Only the published one may appear in the totals.
        attrs = self._attrs("dough")
        self.assertEqual(attrs["total_job_posts"], 1)
        self.assertEqual(attrs["total_applications"], 1)

    def test_trailing_slash_optional(self):
        with_slash = self.client.get("/api/v1/users/dough/application-flow/")
        no_slash = self.client.get("/api/v1/users/dough/application-flow")
        self.assertEqual(with_slash.status_code, 200)
        self.assertEqual(no_slash.status_code, 200)


class TestPublicApplicationFlowVettedOnly(TestCase):
    """CC-107 — the PUBLIC funnel counts ONLY posts the owner has triaged.

    A post is "vetted" iff the OWNER logged at least one "Vetted Good" or
    "Vetted Bad" status on it — an EXISTENCE test (matching ``_vetting_hub``),
    NOT a latest-status check. Unvetted / no-verdict published posts are
    excluded; a verdict by a different user can't pull a post in.
    """

    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name="Bushwood CC")
        self.owner = User.objects.create_user(username="webb", password="pass")
        Profile.objects.create(user=self.owner, federate_rich=True)

        # Vetted Good — counted, terminates at the vetted_good hub (its only
        # status is a triage label, so it yields no "real" application).
        good = self._pub("https://example.com/v/good", "Vetted good role")
        _log(JobApplication.objects.create(job_post=good, user=self.owner),
             "Vetted Good", days_ago=4)

        # Vetted Bad — counted in total_job_posts, terminates at vetted_bad.
        bad = self._pub("https://example.com/v/bad", "Vetted bad role")
        _log(JobApplication.objects.create(job_post=bad, user=self.owner),
             "Vetted Bad", days_ago=4)

        # Unvetted (Applied only, no verdict) — EXCLUDED by the filter.
        unvetted = self._pub("https://example.com/v/unvetted", "Unvetted role")
        _log(JobApplication.objects.create(job_post=unvetted, user=self.owner),
             "Applied", days_ago=3)

        # No verdict AND no application at all — EXCLUDED.
        self._pub("https://example.com/v/bare", "Bare role")

    def _pub(self, link: str, title: str) -> JobPost:
        return JobPost.objects.create(
            created_by=self.owner,
            title=title,
            description=title,
            link=link,
            company=self.company,
            audience=[AS2_PUBLIC],
        )

    def _attrs(self, username: str):
        resp = self.client.get(URL.format(username=username))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["type"], "report")
        self.assertEqual(data["id"], "application-flow")
        attrs = data["attributes"]
        self.assertEqual(set(attrs.keys()), _EXPECTED_ATTR_KEYS)
        self.assertEqual(attrs["scope"], "public_profile")
        return attrs

    def test_counts_only_vetted_posts(self):
        # owner has 4 published posts: 1 Vetted Good + 1 Vetted Bad (counted)
        # and 1 unvetted + 1 no-verdict (excluded). total == good + bad.
        attrs = self._attrs("webb")
        self.assertEqual(attrs["total_job_posts"], 2)

    def test_no_orphan_unvetted_or_stub_nodes(self):
        # With every unvetted post filtered out, build_flow's used_nodes drops
        # the now-orphaned unvetted + stub nodes automatically.
        attrs = self._attrs("webb")
        node_ids = {n["id"] for n in attrs["nodes"]}
        self.assertNotIn("unvetted", node_ids)
        self.assertNotIn("stub", node_ids)

    def test_root_job_posts_node_kept(self):
        # The approved shape is "filter only, keep root" — job_posts stays.
        attrs = self._attrs("webb")
        node_ids = {n["id"] for n in attrs["nodes"]}
        self.assertIn("job_posts", node_ids)

    def test_vetted_then_applied_still_counted_regression(self):
        # THE WHOLE POINT: a post vetted Good then advanced to Applied has
        # LATEST status "Applied" — a latest-status filter would drop it. The
        # existence filter keeps it, and it flows all the way to "applied".
        carl = User.objects.create_user(username="carl", password="pass")
        Profile.objects.create(user=carl, federate_rich=True)
        advanced = JobPost.objects.create(
            created_by=carl,
            title="Assistant greenskeeper",
            description="Advanced post",
            link="https://example.com/v/advanced",
            company=self.company,
            audience=[AS2_PUBLIC],
        )
        app = JobApplication.objects.create(job_post=advanced, user=carl)
        _log(app, "Vetted Good", days_ago=10)  # older verdict
        _log(app, "Applied", days_ago=2)        # newer — latest status

        attrs = self._attrs("carl")
        # Counted despite latest status being "Applied"...
        self.assertEqual(attrs["total_job_posts"], 1)
        self.assertEqual(attrs["total_applications"], 1)
        # ...and it flows PAST the vetting hub all the way to applied.
        self.assertEqual(_edge(attrs, "job_posts", "vetted_good"), 1)
        self.assertEqual(_edge(attrs, "applications", "applied"), 1)

    def test_other_users_verdict_does_not_pull_post_in(self):
        # Owner-scoping: lacey owns an otherwise-unvetted published post; a
        # DIFFERENT user logs "Vetted Good" against it. lacey's funnel must
        # NOT count it — the Exists subquery is scoped to the owner.
        lacey = User.objects.create_user(username="lacey", password="pass")
        Profile.objects.create(user=lacey, federate_rich=True)
        post = JobPost.objects.create(
            created_by=lacey,
            title="Caddy master",
            description="Owner unvetted post",
            link="https://example.com/v/foreign",
            company=self.company,
            audience=[AS2_PUBLIC],
        )
        stranger = User.objects.create_user(username="spackler", password="pass")
        foreign_app = JobApplication.objects.create(job_post=post, user=stranger)
        _log(foreign_app, "Vetted Good", days_ago=1)

        attrs = self._attrs("lacey")
        self.assertEqual(attrs["total_job_posts"], 0)
        self.assertEqual(attrs["nodes"], [])
