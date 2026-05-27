"""Tests for the staff-only GET /api/v1/reports/dedupe-feedback/ endpoint."""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, DuplicateAnnotation, JobPost


User = get_user_model()


class TestDedupeFeedbackReport(TestCase):
    URL = "/api/v1/reports/dedupe-feedback/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.staff = User.objects.create_user(
            username="root", password="pw", is_staff=True
        )
        self.acme = Company.objects.create(name="ACME")
        self.a = JobPost.objects.create(
            title="A", company=self.acme, created_by=self.user,
            link="https://linkedin.com/jobs/1", source="extension",
        )
        self.b = JobPost.objects.create(
            title="B", company=self.acme, created_by=self.user,
            link="https://ats.example.com/2", source="manual",
        )
        self.c = JobPost.objects.create(
            title="C", company=self.acme, created_by=self.user,
            link="https://example.com/3", source="email",
        )

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_empty_when_no_annotations(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["silent_marks"], [])
        self.assertEqual(attrs["canonical_unlinks"], [])
        self.assertEqual(attrs["promote_pairs"], [])

    def test_historical_rows_excluded(self):
        self.client.force_authenticate(user=self.staff)
        DuplicateAnnotation.objects.create(
            from_jp=self.a, to_jp=self.b, action="historical",
            set_by=self.user, signal_state={},
        )
        resp = self.client.get(self.URL)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["totals"]["silent_marks"], 0)
        self.assertEqual(attrs["totals"]["canonical_unlinks"], 0)
        self.assertEqual(attrs["totals"]["promote_pairs"], 0)

    def test_silent_mark_detected_when_candidates_empty(self):
        # Mark fired without any automatic signal — pipeline gap.
        self.client.force_authenticate(user=self.staff)
        DuplicateAnnotation.objects.create(
            from_jp=self.a, to_jp=self.b, action="mark",
            set_by=self.user, signal_state={"candidates": []},
        )
        resp = self.client.get(self.URL)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(len(attrs["silent_marks"]), 1)
        self.assertEqual(attrs["silent_marks"][0]["to_jp_id"], self.b.id)

    def test_mark_not_silent_when_target_in_candidates(self):
        # Pipeline DID surface the target — not a gap.
        self.client.force_authenticate(user=self.staff)
        DuplicateAnnotation.objects.create(
            from_jp=self.a, to_jp=self.b, action="mark",
            set_by=self.user,
            signal_state={"candidates": [
                {"id": self.b.id, "confidence": "high", "signals": ["fingerprint"]},
            ]},
        )
        resp = self.client.get(self.URL)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["silent_marks"], [])

    def test_canonical_unlink_detected(self):
        # Unlink when canonical_link was actively firing → over-eager
        # canonicalization is the likely culprit.
        self.client.force_authenticate(user=self.staff)
        DuplicateAnnotation.objects.create(
            from_jp=self.a, to_jp=None, previous_to=self.b, action="unlink",
            set_by=self.user,
            signal_state={"candidates": [
                {"id": self.b.id, "confidence": "high", "signals": ["canonical_link"]},
            ]},
        )
        resp = self.client.get(self.URL)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(len(attrs["canonical_unlinks"]), 1)
        self.assertEqual(
            attrs["canonical_unlinks"][0]["previous_to_id"], self.b.id
        )

    def test_unlink_without_canonical_signal_excluded(self):
        # Unlink where only fingerprint matched is NOT over-canonical.
        self.client.force_authenticate(user=self.staff)
        DuplicateAnnotation.objects.create(
            from_jp=self.a, to_jp=None, previous_to=self.b, action="unlink",
            set_by=self.user,
            signal_state={"candidates": [
                {"id": self.b.id, "confidence": "high", "signals": ["fingerprint"]},
            ]},
        )
        resp = self.client.get(self.URL)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["canonical_unlinks"], [])

    def test_promote_pairs_capture_source_swap(self):
        self.client.force_authenticate(user=self.staff)
        DuplicateAnnotation.objects.create(
            from_jp=self.b, to_jp=None, previous_to=self.a, action="promote",
            set_by=self.user,
            signal_state={"candidates": []},
        )
        resp = self.client.get(self.URL)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(len(attrs["promote_pairs"]), 1)
        row = attrs["promote_pairs"][0]
        self.assertEqual(row["from_source"], "manual")
        self.assertEqual(row["previous_source"], "extension")
