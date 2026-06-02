"""Phase 5e — federated JobPost visibility filter audit.

Asserts the existing five-clause visibility filter in
``JobPostViewSet.list`` and ``JobPostViewSet.retrieve`` already
excludes federated rows by default — federated rows arrive with
``created_by=NULL`` and no per-user signals (no application, no
score, no scrape, no discovery), so they fail every clause until a
local user creates a JobPostDiscovery linking themselves to the row.

This is the response-shape leg of the four-leg dedupe walk: even
when the ingest decision tree creates a row correctly, it must not
leak into anyone's queryset until the user has opted in.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import JobPost, JobPostDiscovery
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


def _make_federated_jp(*, title="Remote Senior Engineer",
                       link="https://peer.example/jobs/123",
                       source_instance="peer.example") -> JobPost:
    """Convenience: build a row exactly as 5e ingest_create_note would
    persist it. Mirrors lib/federation_ingest.ingest_create_note's
    save-side fields so the test stays representative if the ingest
    code drifts."""
    jp = JobPost(
        title=title,
        description="<p>Remote role posted via federation.</p>",
        link=link,
        source="activitypub",
        source_instance=source_instance,
        audience=[AS2_PUBLIC],
        complete=True,
        created_by=None,  # explicit — federation rows are ownerless
    )
    jp.save()
    return jp


class TestFederatedRowsHiddenByDefault(TestCase):
    """Federated rows must NOT surface in a local user's default queries."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_federated_row_absent_from_list(self):
        fed = _make_federated_jp()
        response = self.client.get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertNotIn(str(fed.id), ids)

    def test_federated_row_not_retrievable(self):
        fed = _make_federated_jp()
        response = self.client.get(f"/api/v1/job-posts/{fed.id}/")
        self.assertEqual(response.status_code, 404)

    def test_local_row_still_appears(self):
        # Regression guard: the visibility filter pre-existed 5e; the
        # ingest path mustn't have introduced a side-effect that
        # excludes local rows belonging to the user.
        local = JobPost.objects.create(
            title="Local Senior Engineer",
            link="https://localco.example/jobs/9",
            created_by=self.user,
            source="manual",
        )
        response = self.client.get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn(str(local.id), ids)

    def test_mixed_queryset_includes_local_excludes_federated(self):
        local = JobPost.objects.create(
            title="Local",
            link="https://localco.example/jobs/1",
            created_by=self.user,
            source="manual",
        )
        federated = _make_federated_jp()
        response = self.client.get("/api/v1/job-posts/")
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn(str(local.id), ids)
        self.assertNotIn(str(federated.id), ids)


class TestDiscoveryOptsUserIn(TestCase):
    """A JobPostDiscovery row makes the federated post visible."""

    def setUp(self):
        self.user = User.objects.create_user(username="bob", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_discovery_makes_federated_row_visible_in_list(self):
        fed = _make_federated_jp()
        JobPostDiscovery.objects.create(
            job_post=fed, user=self.user, source="federation",
        )
        response = self.client.get("/api/v1/job-posts/")
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn(str(fed.id), ids)

    def test_discovery_makes_federated_row_retrievable(self):
        fed = _make_federated_jp()
        JobPostDiscovery.objects.create(
            job_post=fed, user=self.user, source="federation",
        )
        response = self.client.get(f"/api/v1/job-posts/{fed.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["id"], str(fed.id))

    def test_other_users_still_cant_see_via_my_discovery(self):
        fed = _make_federated_jp()
        JobPostDiscovery.objects.create(
            job_post=fed, user=self.user, source="federation",
        )
        # New user — no discovery, no signals
        other = User.objects.create_user(username="carol", password="pw")
        other_client = APIClient()
        other_client.force_authenticate(other)
        response = other_client.get(f"/api/v1/job-posts/{fed.id}/")
        self.assertEqual(response.status_code, 404)


class TestLinkFilterBypassesPerUserGate(TestCase):
    """The extension's filter[link]= canonical lookup intentionally
    bypasses the five-clause filter (see view comment at jobs.py:228).
    Federated rows IS expected to surface in that path — the user is
    providing the URL so they're not enumerating someone else's library.
    This test pins that behavior so a future "tighten the federated
    filter" change doesn't accidentally hide federated rows from the
    "is this URL already tracked?" extension lookup."""

    def setUp(self):
        self.user = User.objects.create_user(username="dave", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_link_filter_returns_federated_row(self):
        fed = _make_federated_jp(link="https://peer.example/jobs/abc")
        response = self.client.get(
            "/api/v1/job-posts/", {"filter[link]": "https://peer.example/jobs/abc"},
        )
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn(str(fed.id), ids)


class TestStaffSeesAllFederatedRows(TestCase):
    """Staff bypasses the per-user filter on list — federated rows show."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="root", password="pw", is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_staff_sees_federated_row_in_list(self):
        fed = _make_federated_jp()
        response = self.client.get("/api/v1/job-posts/")
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn(str(fed.id), ids)
