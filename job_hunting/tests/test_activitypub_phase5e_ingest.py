"""Phase 5e — federated JobPost ingestion + dedup tests.

Covers ``lib/federation_ingest.ingest_create_note`` directly + the
inbox handler wiring that calls it on inbound Create(Note) activities.

The signing layer is not the point of these tests; the inbox wiring
test that DOES exercise it reuses the 5c FakeRemoteActor harness.
Module-level ingest tests build activity dicts in-line and call the
function directly so we get full control over the decision tree.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from job_hunting.lib import federation_ingest
from job_hunting.lib.federation_ingest import (
    OUTCOME_CREATED,
    OUTCOME_MERGED,
    OUTCOME_REJECTED,
    OUTCOME_SKIPPED,
    ingest_create_note,
    replay_inbound_creates,
)
from job_hunting.models import (
    DuplicateAnnotation,
    FederationActivity,
    JobPost,
)
from job_hunting.models.job_post import AS2_PUBLIC


User = get_user_model()


# ---------------------------------------------------------------------------
# Activity / audit-row factories.
# ---------------------------------------------------------------------------


def _activity(
    *,
    activity_id: str = "https://peer.example/activities/create-1",
    actor: str = "https://peer.example/users/alice",
    object_id: str = "https://peer.example/notes/1",
    note_url: str = "https://hire.example/jobs/123",
    note_type: str = "Note",
    title: str = "Senior Federated Engineer",
    content: str = "<p>Cool federated role at Acme.</p>",
    published: str = "2026-05-01T12:00:00Z",
    public: bool = True,
    extra_object: dict | None = None,
) -> dict:
    """Build an inbound Create(Note) activity dict.

    Default carries AS2 Public on the envelope (`to`); ``public=False``
    flips that so the "private content snuck through" rejection branch
    can be exercised cleanly.
    """
    body = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": activity_id,
        "type": "Create",
        "actor": actor,
        "object": {
            "id": object_id,
            "type": note_type,
            "name": title,
            "content": content,
            "url": note_url,
            "published": published,
            "attributedTo": actor,
        },
    }
    if public:
        body["to"] = [AS2_PUBLIC]
    if extra_object:
        body["object"].update(extra_object)
    return body


def _audit_row(activity: dict) -> FederationActivity:
    """Persist a 5c-style inbound audit row for the activity, then
    return it. Mirrors what ``_log_inbound`` writes from the inbox
    handler — delivery_status starts as ``accepted``.
    """
    return FederationActivity.objects.create(
        direction="inbound",
        activity_type="Create",
        activity_id=activity["id"],
        actor_uri=activity["actor"],
        target_uri=activity["object"]["id"],
        body=json.dumps(activity),
        signature_payload="fake-sig-header",
        received_at=datetime.now(tz=timezone.utc),
        delivery_status="accepted",
    )


# ---------------------------------------------------------------------------
# Happy path — create + merge + skipped + audit semantics.
# ---------------------------------------------------------------------------


class TestIngestCreate(TestCase):
    def setUp(self):
        cache.clear()

    def test_valid_create_inserts_jobpost(self):
        activity = _activity()
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertIsNotNone(result.job_post)
        jp = result.job_post
        self.assertEqual(jp.source, "federation")
        self.assertEqual(jp.source_instance, "peer.example")
        self.assertEqual(jp.audience, [AS2_PUBLIC])
        self.assertIsNone(jp.created_by_id)
        self.assertTrue(jp.complete)

    def test_created_row_canonical_link_is_normalized(self):
        # Default canonicalizer strips utm_*; pin that the federated
        # write path runs through the same normalization, not a
        # bypass.
        activity = _activity(
            note_url="https://hire.example/jobs/123?utm_source=mastodon",
        )
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(
            result.job_post.canonical_link,
            "https://hire.example/jobs/123",
        )

    def test_title_falls_back_to_first_content_line(self):
        # ``object.name`` absent → first content line, truncated to 200.
        activity = _activity(content="Quick microblog title line\nthen body")
        activity["object"].pop("name", None)
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(result.job_post.title, "Quick microblog title line")

    def test_posted_date_set_from_published(self):
        activity = _activity(published="2025-12-15T08:30:00Z")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(result.job_post.posted_date.isoformat(), "2025-12-15")

    def test_article_type_also_accepted(self):
        # Article is a Plume/WriteFreely variant — same dedup path.
        activity = _activity(note_type="Article")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)

    def test_audit_row_stays_accepted_on_create(self):
        activity = _activity()
        row = _audit_row(activity)
        ingest_create_note(activity, federation_activity=row)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, "accepted")


class TestIngestMerge(TestCase):
    """Canonical-link duplicate → MERGE, audit annotation written."""

    def setUp(self):
        cache.clear()
        self.local = JobPost.objects.create(
            title="Local prior version",
            link="https://hire.example/jobs/123",
            source="manual",
        )

    def test_canonical_link_match_merges(self):
        activity = _activity(note_url="https://hire.example/jobs/123")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_MERGED)
        self.assertEqual(result.job_post.id, self.local.id)

    def test_merge_does_not_create_new_jobpost(self):
        before = JobPost.objects.count()
        activity = _activity(note_url="https://hire.example/jobs/123")
        row = _audit_row(activity)
        ingest_create_note(activity, federation_activity=row)
        self.assertEqual(JobPost.objects.count(), before)

    def test_merge_writes_federated_merge_annotation(self):
        activity = _activity(note_url="https://hire.example/jobs/123")
        row = _audit_row(activity)
        ingest_create_note(activity, federation_activity=row)
        annot = DuplicateAnnotation.objects.get(
            action=DuplicateAnnotation.FEDERATED_MERGE,
        )
        self.assertEqual(annot.from_jp_id, self.local.id)
        self.assertEqual(annot.to_jp_id, self.local.id)
        self.assertIsNone(annot.set_by_id)
        self.assertEqual(
            annot.signal_state["federation"]["activity_id"],
            activity["id"],
        )
        self.assertEqual(
            annot.signal_state["federation"]["actor_uri"],
            activity["actor"],
        )

    def test_sticky_closed_local_rejects_merge(self):
        self.local.posting_status = "closed"
        self.local.save(update_fields=["posting_status"])
        activity = _activity(note_url="https://hire.example/jobs/123")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "sticky_closed_local")
        # No annotation, no merge.
        self.assertEqual(
            DuplicateAnnotation.objects.filter(
                action=DuplicateAnnotation.FEDERATED_MERGE
            ).count(),
            0,
        )

    def test_idempotent_replay_keeps_one_jp(self):
        # Same activity twice → first creates (or merges), second
        # hits the same canonical_link branch.
        # Use a previously-empty canonical_link so we exercise CREATE then MERGE.
        JobPost.objects.all().delete()
        activity = _activity(note_url="https://hire.example/jobs/new-one")
        row1 = _audit_row(activity)
        result1 = ingest_create_note(activity, federation_activity=row1)
        self.assertEqual(result1.outcome, OUTCOME_CREATED)
        # Same activity, fresh audit row (replay through the audit log).
        activity2 = dict(activity)
        activity2["id"] = activity["id"] + "-replay"
        row2 = _audit_row(activity2)
        result2 = ingest_create_note(activity2, federation_activity=row2)
        self.assertEqual(result2.outcome, OUTCOME_MERGED)
        self.assertEqual(
            JobPost.objects.filter(source="federation").count(), 1
        )


class TestIngestSkipped(TestCase):
    """Non-Note object types skip cleanly without writing JobPost."""

    def setUp(self):
        cache.clear()

    def test_image_object_skipped(self):
        activity = _activity(note_type="Image")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_SKIPPED)
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 0)

    def test_skipped_does_not_demote_audit_row(self):
        activity = _activity(note_type="Audio")
        row = _audit_row(activity)
        ingest_create_note(activity, federation_activity=row)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, "accepted")


# ---------------------------------------------------------------------------
# Defensive parsing — every rejection verdict.
# ---------------------------------------------------------------------------


class TestIngestRejections(TestCase):
    def setUp(self):
        cache.clear()

    def test_object_not_a_dict_rejects(self):
        activity = _activity()
        activity["object"] = "https://peer.example/notes/1"  # bare string
        row = _audit_row({**activity, "object": {"id": "x", "type": "Note"}})
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "object_not_object")

    def test_missing_audience_public_rejects(self):
        activity = _activity(public=False)
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "not_public")

    def test_oversized_content_rejects(self):
        # Override the cap low so we don't have to allocate megabytes
        # of test data; ingest reads the configured limit at call time.
        with override_settings(ACTIVITYPUB_INGEST_BODY_MAX_BYTES=128):
            activity = _activity(content="x" * 1024)
            row = _audit_row(activity)
            result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "content_too_large")

    def test_missing_url_rejects(self):
        activity = _activity()
        activity["object"].pop("url")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertTrue(result.reason.startswith("schema:url"))

    def test_malformed_url_rejects(self):
        activity = _activity(note_url="not-a-url")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertTrue(result.reason.startswith("schema:url"))

    def test_missing_published_rejects(self):
        activity = _activity()
        activity["object"].pop("published")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertTrue(result.reason.startswith("schema:published"))

    def test_malformed_published_rejects(self):
        activity = _activity(published="not-iso-8601")
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertTrue(result.reason.startswith("schema:published"))

    def test_missing_instance_host_rejects(self):
        # attributedTo + actor both missing → no host to attribute.
        activity = _activity()
        activity["object"].pop("attributedTo")
        activity.pop("actor")
        row = _audit_row({**activity, "actor": "https://peer.example/u/x"})
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "missing_instance_host")

    def test_rejected_demotes_audit_row(self):
        activity = _activity(public=False)
        row = _audit_row(activity)
        ingest_create_note(activity, federation_activity=row)
        row.refresh_from_db()
        self.assertEqual(row.delivery_status, "rejected")
        self.assertIn("not_public", row.delivery_error)


# ---------------------------------------------------------------------------
# Per-instance quota.
# ---------------------------------------------------------------------------


@override_settings(ACTIVITYPUB_INGEST_INSTANCE_QUOTA_PER_HOUR=2)
class TestIngestQuota(TestCase):
    def setUp(self):
        cache.clear()

    def test_quota_caps_creates(self):
        for i in range(2):
            activity = _activity(
                activity_id=f"https://peer.example/activities/q-{i}",
                note_url=f"https://hire.example/jobs/q-{i}",
            )
            row = _audit_row(activity)
            result = ingest_create_note(activity, federation_activity=row)
            self.assertEqual(result.outcome, OUTCOME_CREATED)
        # Third create from same host → quota exhausted.
        activity = _activity(
            activity_id="https://peer.example/activities/q-3",
            note_url="https://hire.example/jobs/q-3",
        )
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "instance_quota_exceeded")

    def test_quota_does_not_count_merges(self):
        # Seed a row first so subsequent activities hit MERGE.
        JobPost.objects.create(
            title="Prior", link="https://hire.example/jobs/m-1", source="manual",
        )
        # Three merges in a row — should NOT decrement the quota.
        # Each activity needs a unique id (audit-row uniqueness on
        # (direction, activity_id, target_uri) — same constraint as
        # 5c replay protection).
        for i in range(3):
            activity = _activity(
                activity_id=f"https://peer.example/activities/merge-{i}",
                object_id=f"https://peer.example/notes/merge-{i}",
                note_url="https://hire.example/jobs/m-1",
            )
            row = _audit_row(activity)
            result = ingest_create_note(activity, federation_activity=row)
            self.assertEqual(result.outcome, OUTCOME_MERGED)
        # Bucket still available → fresh create from same host works.
        activity = _activity(
            activity_id="https://peer.example/activities/postmerge-1",
            object_id="https://peer.example/notes/postmerge-1",
            note_url="https://hire.example/jobs/fresh-1",
        )
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)

    def test_quota_isolates_per_host(self):
        # Fill peer.example
        for i in range(2):
            activity = _activity(
                activity_id=f"https://peer.example/activities/iso-{i}",
                note_url=f"https://hire.example/jobs/iso-{i}",
            )
            row = _audit_row(activity)
            ingest_create_note(activity, federation_activity=row)
        # other.example can still create
        activity = _activity(
            actor="https://other.example/users/bob",
            activity_id="https://other.example/activities/iso-1",
            note_url="https://hire.example/jobs/iso-other",
        )
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_CREATED)


# ---------------------------------------------------------------------------
# Kill-switch + replay tool.
# ---------------------------------------------------------------------------


@override_settings(ACTIVITYPUB_INGEST_ENABLED=False)
class TestIngestDisabled(TestCase):
    def setUp(self):
        cache.clear()

    def test_disabled_skips_without_creating(self):
        activity = _activity()
        row = _audit_row(activity)
        result = ingest_create_note(activity, federation_activity=row)
        self.assertEqual(result.outcome, OUTCOME_SKIPPED)
        self.assertEqual(result.reason, "ingest_disabled")
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 0)


class TestReplayTool(TestCase):
    def setUp(self):
        cache.clear()

    def test_replay_processes_logged_only_creates(self):
        # Simulate 5c-era log-only operation: audit rows but no
        # ingestion has happened.
        for i in range(3):
            activity = _activity(
                activity_id=f"https://peer.example/activities/replay-{i}",
                note_url=f"https://hire.example/jobs/replay-{i}",
            )
            _audit_row(activity)
        tally = replay_inbound_creates(limit=10)
        self.assertEqual(tally[OUTCOME_CREATED], 3)
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 3)


# ---------------------------------------------------------------------------
# Inbox integration — the wired-up handler calls ingest.
# ---------------------------------------------------------------------------


TEST_ORIGIN = "http://testserver"


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboxCallsIngest(TestCase):
    """Wired path: POST signed Create(Note) to /actors/<u>/inbox →
    audit row written by 5c AND ingest_create_note called by 5e."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough", password="pw")
        from job_hunting.models import Actor
        Actor.objects.create(
            preferred_username="dough", type="Person", user=self.user,
        )
        from job_hunting.tests.test_activitypub_phase5c_signing import (
            FakeRemoteActor,
        )
        self.peer = FakeRemoteActor()
        self.path = "/actors/dough/inbox"

    def _post(self, body: bytes):
        from job_hunting.lib import federation_signing
        headers = self.peer.sign_request("POST", self.path, body, "testserver")
        with patch.object(
            federation_signing, "fetch_actor_public_key",
            return_value=self.peer.public_pem,
        ):
            return self.client.post(
                self.path, data=body,
                content_type="application/activity+json",
                **{f"HTTP_{k.upper().replace('-', '_')}": v
                   for k, v in headers.items() if k.lower() != "host"},
                HTTP_HOST="testserver",
            )

    def _create_note_payload(self, **kwargs) -> bytes:
        defaults = {
            "activity_id": f"{self.peer.actor_uri}/activities/inbox-create-1",
            "actor": self.peer.actor_uri,
            "object_id": f"{self.peer.actor_uri}/notes/1",
            "note_url": "https://hire.example/jobs/inbox-1",
        }
        defaults.update(kwargs)
        return json.dumps(_activity(**defaults)).encode("utf-8")

    def test_inbox_create_runs_ingest_and_creates_jobpost(self):
        body = self._create_note_payload()
        response = self._post(body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 1)
        jp = JobPost.objects.get(source="federation")
        # Source instance taken from the peer actor's host.
        from urllib.parse import urlparse
        self.assertEqual(
            jp.source_instance,
            urlparse(self.peer.actor_uri).netloc.lower(),
        )

    def test_inbox_create_writes_audit_row(self):
        body = self._create_note_payload()
        self._post(body)
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Create",
            ).exists()
        )

    def test_inbox_returns_202_even_when_ingest_rejects(self):
        # private content (no Public audience) → ingest rejects
        # but the inbox still returns 202.
        payload = _activity(
            activity_id=f"{self.peer.actor_uri}/activities/inbox-private-1",
            actor=self.peer.actor_uri,
            note_url="https://hire.example/jobs/private",
            public=False,
        )
        body = json.dumps(payload).encode("utf-8")
        response = self._post(body)
        self.assertEqual(response.status_code, 202)
        # Audit row demoted to rejected.
        row = FederationActivity.objects.get(
            direction="inbound", activity_id=payload["id"],
        )
        self.assertEqual(row.delivery_status, "rejected")
        self.assertIn("not_public", row.delivery_error)
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 0)

    @override_settings(ACTIVITYPUB_INGEST_ENABLED=False)
    def test_inbox_create_with_ingest_disabled_logs_but_no_jobpost(self):
        body = self._create_note_payload()
        response = self._post(body)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            FederationActivity.objects.filter(
                direction="inbound", activity_type="Create",
            ).exists()
        )
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 0)


# ---------------------------------------------------------------------------
# Sanity: ingest module's outcome constants are stable strings.
# ---------------------------------------------------------------------------


class TestModuleConstants(TestCase):
    def test_outcomes_are_lowercase_strings(self):
        self.assertEqual(OUTCOME_CREATED, "created")
        self.assertEqual(OUTCOME_MERGED, "merged")
        self.assertEqual(OUTCOME_REJECTED, "rejected")
        self.assertEqual(OUTCOME_SKIPPED, "skipped")

    def test_replay_handles_unparseable_body(self):
        # Audit row with garbage JSON shouldn't crash the replay loop.
        FederationActivity.objects.create(
            direction="inbound", activity_type="Create",
            activity_id="https://peer.example/activities/garbage",
            actor_uri="https://peer.example/users/x",
            body="this is not json",
            delivery_status="accepted",
        )
        tally = replay_inbound_creates(limit=10)
        self.assertEqual(tally["error"], 1)


# Silence the unused-import lint trigger for ``federation_ingest`` (used
# implicitly via direct symbol imports above; module name kept available
# in case tests want to monkeypatch at the module level).
_ = federation_ingest
_ = format_datetime  # imported for parity with 5c tests; kept for parity hooks
