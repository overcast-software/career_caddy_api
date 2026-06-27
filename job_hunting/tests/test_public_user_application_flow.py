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

        # Rich owner: opted into federate_rich, with one PUBLISHED post that
        # carries a real application (Applied) so totals are non-zero, plus a
        # PRIVATE post that also has an application — it must never count.
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
