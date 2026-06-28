"""CC-112 — ``meta.federation.timeline`` on the public /@dough federated feed.

The CC-51 public federated collection
(``GET /api/v1/users/<username>/job-posts/federated/``) carries the owner's
JobApplication status history under ``meta.federation.timeline`` = ascending
``[{status, at}]`` (status NAME + ``logged_at`` ISO) for the rich /@dough line
chart (sibling frontend ticket #3b). The block rides the SAME
``Profile.federate_rich`` gate as the rest of ``meta.federation``; #3a is
api-only.

Privacy is load-bearing: timeline entries expose ONLY the status NAME and its
timestamp — never ``reason_code`` / ``note`` (on the status row) or
``tracking_url`` (on the application) — and the whole ``meta.federation`` block
is ABSENT when the owner has ``federate_rich`` off.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
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

URL = "/api/v1/users/dough/job-posts/federated/"
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_timezone.utc)


def _status(name):
    return Status.objects.get_or_create(status=name)[0]


@override_settings(
    INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver"
)
class TestFederationTimelineRich(TestCase):
    """federate_rich=True → each post carries an ascending status timeline."""

    def setUp(self):
        self.owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=self.owner, federate_rich=True)
        self.company = Company.objects.create(name="Bushwood CC")

    def _post(self, n="1"):
        return JobPost.objects.create(
            created_by=self.owner,
            title=f"Engineer {n}",
            description="d",
            complete=True,
            link=f"https://x.example/j/{n}",
            company=self.company,
            audience=[AS2_PUBLIC],
        )

    def _log(self, app, name, at):
        return JobApplicationStatus.objects.create(
            application=app, status=_status(name), logged_at=at
        )

    def _timeline(self):
        body = APIClient().get(URL).json()
        return body["data"][0]["meta"]["federation"]["timeline"]

    def test_timeline_ascending_status_and_at(self):
        post = self._post()
        app = JobApplication.objects.create(job_post=post, user=self.owner)
        # Logged out of order — the feed must return them ascending by time.
        self._log(app, "Interview", BASE + timedelta(days=2))
        self._log(app, "Applied", BASE + timedelta(days=1))
        self._log(app, "Vetted Good", BASE)
        timeline = self._timeline()
        self.assertEqual(
            [e["status"] for e in timeline],
            ["Vetted Good", "Applied", "Interview"],
        )
        ats = [e["at"] for e in timeline]
        self.assertEqual(ats, sorted(ats))
        self.assertEqual(timeline[0]["at"], BASE.isoformat())

    def test_merges_multiple_applications_for_one_post(self):
        post = self._post()
        app1 = JobApplication.objects.create(job_post=post, user=self.owner)
        app2 = JobApplication.objects.create(job_post=post, user=self.owner)
        self._log(app1, "Applied", BASE + timedelta(days=1))
        self._log(app2, "Interview", BASE + timedelta(days=3))
        self._log(app2, "Vetted Good", BASE)
        timeline = self._timeline()
        # Two applications' rows merge into ONE ascending timeline.
        self.assertEqual(
            [e["status"] for e in timeline],
            ["Vetted Good", "Applied", "Interview"],
        )

    def test_dedupes_identical_status_at_pairs(self):
        post = self._post()
        app1 = JobApplication.objects.create(job_post=post, user=self.owner)
        app2 = JobApplication.objects.create(job_post=post, user=self.owner)
        # Same (status, at) across two applications collapses to one point.
        self._log(app1, "Applied", BASE)
        self._log(app2, "Applied", BASE)
        timeline = self._timeline()
        self.assertEqual(
            timeline, [{"status": "Applied", "at": BASE.isoformat()}]
        )

    def test_at_falls_back_to_created_at_when_logged_at_null(self):
        post = self._post()
        app = JobApplication.objects.create(job_post=post, user=self.owner)
        jas = JobApplicationStatus.objects.create(
            application=app, status=_status("Applied"), logged_at=None
        )
        stamp = BASE + timedelta(days=5)
        # created_at is auto_now_add; .update() bypasses it for a fixed stamp.
        JobApplicationStatus.objects.filter(pk=jas.pk).update(created_at=stamp)
        timeline = self._timeline()
        self.assertEqual(
            timeline, [{"status": "Applied", "at": stamp.isoformat()}]
        )

    def test_post_with_no_applications_has_empty_timeline(self):
        self._post()
        # Rich on → the federation block is present, timeline an empty list.
        fed = APIClient().get(URL).json()["data"][0]["meta"]["federation"]
        self.assertEqual(fed["timeline"], [])

    def test_only_owner_applications_count(self):
        # A different user's application/status on the same shared post must
        # NOT bleed into the owner's public timeline.
        post = self._post()
        other = User.objects.create_user(username="judge", password="p")
        other_app = JobApplication.objects.create(job_post=post, user=other)
        self._log(other_app, "Applied", BASE)
        self.assertEqual(self._timeline(), [])

    def test_timeline_query_count_constant(self):
        # Two posts each with a status row.
        for n in range(1, 3):
            post = self._post(str(n))
            app = JobApplication.objects.create(job_post=post, user=self.owner)
            self._log(app, "Applied", BASE)
        client = APIClient()
        with CaptureQueriesContext(connection) as small:
            client.get(URL)
        q_small = len(small)
        # Five more posts, each with two status rows — must not add queries.
        for n in range(3, 8):
            post = self._post(str(n))
            app = JobApplication.objects.create(job_post=post, user=self.owner)
            self._log(app, "Applied", BASE + timedelta(days=n))
            self._log(app, "Interview", BASE + timedelta(days=n + 1))
        with CaptureQueriesContext(connection) as big:
            client.get(URL)
        q_big = len(big)
        self.assertEqual(
            q_small, q_big, f"timeline N+1'd: {q_small} (2 posts) vs {q_big} (7)"
        )


@override_settings(
    INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver"
)
class TestFederationTimelinePrivacy(TestCase):
    """Hard privacy: timeline entries are {status, at} ONLY, and the whole
    ``meta.federation`` block disappears when ``federate_rich`` is off."""

    NOTE_SENTINEL = "PRIVATE_NOTE_DO_NOT_LEAK_xyz"
    REASON_SENTINEL = "PRIVATE_REASON_xyz"
    TRACKING_SENTINEL = "https://tracking.example/PRIVATE_xyz"

    def _seed_post(self, owner):
        company = Company.objects.create(name="Bushwood CC")
        post = JobPost.objects.create(
            created_by=owner,
            title="Engineer",
            description="d",
            complete=True,
            link="https://x.example/j/1",
            company=company,
            audience=[AS2_PUBLIC],
        )
        app = JobApplication.objects.create(
            job_post=post, user=owner, tracking_url=self.TRACKING_SENTINEL
        )
        # Vetted row carries a private free-text note; reason_code left NULL so
        # the legitimately-public verdict_reason_code field stays empty and the
        # note sentinel is the only private value on this row.
        JobApplicationStatus.objects.create(
            application=app,
            status=_status("Vetted Good"),
            logged_at=BASE,
            note=self.NOTE_SENTINEL,
        )
        # A non-verdict row carries a sentinel reason_code. The verdict path
        # reads reason_code ONLY from Vetted Good/Bad rows, so this value must
        # never surface anywhere in the public payload.
        JobApplicationStatus.objects.create(
            application=app,
            status=_status("Applied"),
            logged_at=BASE + timedelta(days=1),
            reason_code=self.REASON_SENTINEL,
        )
        return post

    def test_timeline_entries_have_only_status_and_at(self):
        owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=owner, federate_rich=True)
        self._seed_post(owner)
        timeline = APIClient().get(URL).json()["data"][0]["meta"]["federation"][
            "timeline"
        ]
        self.assertTrue(timeline)
        for entry in timeline:
            self.assertEqual(set(entry.keys()), {"status", "at"})

    def test_private_values_never_appear_in_payload(self):
        owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=owner, federate_rich=True)
        self._seed_post(owner)
        raw = json.dumps(APIClient().get(URL).json())
        self.assertNotIn(self.NOTE_SENTINEL, raw)
        self.assertNotIn(self.REASON_SENTINEL, raw)
        self.assertNotIn(self.TRACKING_SENTINEL, raw)

    def test_block_absent_when_federate_rich_off_anonymous(self):
        owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=owner, federate_rich=False)
        self._seed_post(owner)
        # Anonymous visitor to a non-rich profile: NO federation block at all,
        # therefore no timeline.
        resource = APIClient().get(URL).json()["data"][0]
        self.assertNotIn("federation", resource.get("meta", {}))

    def test_block_absent_when_federate_rich_off_non_owner(self):
        owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=owner, federate_rich=False)
        self._seed_post(owner)
        other = User.objects.create_user(username="judge", password="p")
        client = APIClient()
        client.force_authenticate(user=other)
        resource = client.get(URL).json()["data"][0]
        self.assertNotIn("federation", resource.get("meta", {}))
