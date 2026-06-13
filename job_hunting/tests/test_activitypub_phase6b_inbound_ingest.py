"""Phase 6b — inbound JP ingest with cross-instance dedupe.

Covers the new Phase 6b surfaces layered onto the existing 5e ingest:

- ``careercaddy:extension`` AS2 coercion (apply_url, company, canonical_link, posting_status).
- ``Company`` resolution: name_slug match → existing row;
  miss → fresh row with ``source="federation"``, ``federation_enabled=False``.
- HTML sanitization on description, length caps on title + description.
- Per-actor / per-instance rate cap (already shared with 5e; sanity here).
- ``inReplyTo`` Create → SKIPPED (Phase 7c placeholder).
- ``Update(Note)`` merges empty fields only; never clobbers non-empty.
- ``Delete`` now flips ``posting_status="closed"`` and ``complete=False``
  in addition to the existing ``source_deleted_at`` tombstone.
- Cross-instance Delete still no-ops.
- Five-clause visibility — federated rows hidden by default,
  visible after a JobPostDiscovery row exists.

The 5e tests in ``test_activitypub_phase5e_ingest.py`` already cover
the canonical-link dedupe + audit semantics; this module focuses on
the field-level surface 6b adds.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.lib import federation_ingest
from job_hunting.lib.federation_ingest import (
    OUTCOME_CREATED,
    OUTCOME_MERGED,
    OUTCOME_REJECTED,
    OUTCOME_SKIPPED,
    ingest_create_note,
    ingest_update_note,
)
from job_hunting.models import (
    Actor,
    Company,
    DuplicateAnnotation,
    FederationActivity,
    FederationFollower,
    JobPost,
    JobPostDiscovery,
)
from job_hunting.models.job_post import AS2_PUBLIC
from job_hunting.tests.test_activitypub_phase5c_signing import FakeRemoteActor


User = get_user_model()


TEST_ORIGIN = "http://testserver"
PEER_HOST = "peer.example"


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
    in_reply_to: str | None = None,
    extension: dict | None = None,
    attributed_to: str | None = None,
) -> dict:
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
            "attributedTo": attributed_to or actor,
        },
    }
    if public:
        body["to"] = [AS2_PUBLIC]
    if in_reply_to is not None:
        body["object"]["inReplyTo"] = in_reply_to
    if extension is not None:
        body["object"]["careercaddy:extension"] = extension
    return body


def _audit_row(activity: dict) -> FederationActivity:
    """Persist a 5c-style inbound audit row mirroring the inbox handler."""
    return FederationActivity.objects.create(
        direction="inbound",
        activity_type=activity["type"],
        activity_id=activity["id"],
        actor_uri=activity["actor"],
        target_uri=(
            activity["object"]["id"]
            if isinstance(activity["object"], dict)
            else activity["object"]
        ),
        body=json.dumps(activity),
        signature_payload="fake-sig-header",
        received_at=datetime.now(tz=timezone.utc),
        delivery_status="accepted",
    )


# ---------------------------------------------------------------------------
# careercaddy:extension coercion — apply_url + posting_status + canonical_link.
# ---------------------------------------------------------------------------


class TestExtensionCoercion(TestCase):
    def setUp(self):
        cache.clear()

    def test_extension_apply_url_populated_on_create(self):
        activity = _activity(
            extension={
                "apply_url": "https://greenhouse.io/acme/jobs/abc",
            },
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(
            result.job_post.apply_url,
            "https://greenhouse.io/acme/jobs/abc",
        )

    def test_extension_posting_status_populated_on_create(self):
        activity = _activity(extension={"posting_status": "closed"})
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(result.job_post.posting_status, "closed")

    def test_extension_invalid_posting_status_dropped(self):
        activity = _activity(extension={"posting_status": "rescinded"})
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        # Defensive — silently drop anything outside the choices set so
        # we don't write a value the model's POSTING_STATUS_CHOICES
        # constraint rejects on serialization.
        self.assertIsNone(result.job_post.posting_status)

    def test_extension_canonical_link_overrides_note_url(self):
        # The extension canonical is a more specific form (post-rewrite,
        # post-tracking-strip) than the bare URL; peer is asserting that
        # form as the dedupe key.
        activity = _activity(
            note_url="https://hire.example/jobs/123?utm_source=fed",
            extension={
                "canonical_link": "https://hire.example/jobs/123",
            },
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(
            result.job_post.canonical_link,
            "https://hire.example/jobs/123",
        )

    def test_extension_blank_canonical_falls_back_to_note_url(self):
        activity = _activity(
            note_url="https://hire.example/jobs/123",
            extension={"canonical_link": ""},
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(
            result.job_post.canonical_link,
            "https://hire.example/jobs/123",
        )


# ---------------------------------------------------------------------------
# Company resolution.
# ---------------------------------------------------------------------------


class TestCompanyResolution(TestCase):
    def setUp(self):
        cache.clear()

    def test_company_name_slug_match_reuses_existing(self):
        # Phase A name_slug is slug(strip_corp_suffix(name)).
        from job_hunting.lib.slug import slug, strip_corp_suffix
        existing = Company.objects.create(
            name="Acme Inc.",
            name_slug=slug(strip_corp_suffix("Acme Inc.")),
            source="manual",
        )
        activity = _activity(extension={"company": "Acme"})
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(result.job_post.company_id, existing.id)

    def test_company_miss_creates_fresh_with_source_federation(self):
        activity = _activity(extension={"company": "BrandNewCo"})
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertIsNotNone(result.job_post.company_id)
        company = Company.objects.get(pk=result.job_post.company_id)
        self.assertEqual(company.name, "BrandNewCo")
        self.assertEqual(company.source, Company.SOURCE_FEDERATION)
        self.assertFalse(company.federation_enabled)

    def test_no_company_field_creates_jp_without_company(self):
        activity = _activity()
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertIsNone(result.job_post.company_id)


# ---------------------------------------------------------------------------
# Sanitization + length caps.
# ---------------------------------------------------------------------------


class TestSanitization(TestCase):
    def setUp(self):
        cache.clear()

    def test_html_in_description_stripped(self):
        # Smuggled <script> + an event handler + a benign <p> — all
        # tags strip out, content text survives.
        activity = _activity(
            content='<p>Hello</p><script>alert(1)</script><div onclick="x()">world</div>',
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertNotIn("<script>", result.job_post.description)
        self.assertNotIn("onclick", result.job_post.description)
        self.assertIn("Hello", result.job_post.description)
        self.assertIn("world", result.job_post.description)

    def test_title_clipped_at_255(self):
        long_title = "X" * 500
        activity = _activity(title=long_title)
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(len(result.job_post.title), 255)

    def test_description_clipped_at_50kb(self):
        # 60 KB of content → trimmed to 50 KB ceiling.
        activity = _activity(content="x" * (60 * 1024))
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertLessEqual(
            len(result.job_post.description.encode("utf-8")), 50 * 1024
        )


# ---------------------------------------------------------------------------
# Source enum + base shape.
# ---------------------------------------------------------------------------


class TestSourceFederation(TestCase):
    def setUp(self):
        cache.clear()

    def test_created_row_source_is_federation(self):
        activity = _activity()
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.assertEqual(result.job_post.source, "federation")


# ---------------------------------------------------------------------------
# Per-actor / per-instance rate cap (default 100 per hour).
# ---------------------------------------------------------------------------


@override_settings(ACTIVITYPUB_INGEST_INSTANCE_QUOTA_PER_HOUR=100)
class TestRateCapDefault(TestCase):
    def setUp(self):
        cache.clear()

    def test_101st_create_in_hour_rejected(self):
        # 100 successful creates, all hitting the cap. We don't need to
        # fully exercise 100 distinct rows in the DB — we just need the
        # cache counter to walk past 100. Use 100 distinct URLs so each
        # iteration takes the CREATE branch.
        for i in range(100):
            activity = _activity(
                activity_id=f"https://peer.example/activities/cap-{i}",
                object_id=f"https://peer.example/notes/cap-{i}",
                note_url=f"https://hire.example/jobs/cap-{i}",
            )
            result = ingest_create_note(activity, federation_activity=_audit_row(activity))
            self.assertEqual(result.outcome, OUTCOME_CREATED)
        # 101st in the same hour from the same instance → rejected.
        activity = _activity(
            activity_id="https://peer.example/activities/cap-overflow",
            object_id="https://peer.example/notes/cap-overflow",
            note_url="https://hire.example/jobs/cap-overflow",
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "instance_quota_exceeded")


# ---------------------------------------------------------------------------
# inReplyTo Create → SKIPPED (Phase 7c placeholder).
# ---------------------------------------------------------------------------


class TestInReplyToNoOp(TestCase):
    def setUp(self):
        cache.clear()

    def test_in_reply_to_skipped(self):
        # A reply-shaped Note isn't a JP candidate. Phase 7c will route
        # these to a JobPostComment ingest path; today they SKIP cleanly
        # so the audit row stays accepted without manifesting a JP.
        activity = _activity(
            in_reply_to="https://peer.example/notes/some-jp",
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_SKIPPED)
        self.assertEqual(result.reason, "in_reply_to_not_yet_supported")
        self.assertEqual(JobPost.objects.filter(source="federation").count(), 0)


# ---------------------------------------------------------------------------
# Inbound Update — merge-empty-fields only.
# ---------------------------------------------------------------------------


class TestInboundUpdate(TestCase):
    def setUp(self):
        cache.clear()
        self.peer_host = PEER_HOST
        # Pre-existing federated row to update.
        activity = _activity(
            note_url="https://hire.example/jobs/upd-1",
            title="Original title",
            content="Original description.",
        )
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.jp = result.job_post

    def _update_activity(self, *, title=None, content=None, extension=None):
        body = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://peer.example/activities/update-1",
            "type": "Update",
            "actor": "https://peer.example/users/alice",
            "to": [AS2_PUBLIC],
            "object": {
                "id": "https://peer.example/notes/1",
                "type": "Note",
                "url": "https://hire.example/jobs/upd-1",
                "attributedTo": "https://peer.example/users/alice",
            },
        }
        if title is not None:
            body["object"]["name"] = title
        if content is not None:
            body["object"]["content"] = content
        if extension is not None:
            body["object"]["careercaddy:extension"] = extension
        return body

    def test_update_merges_empty_apply_url(self):
        # JP has no apply_url. Update fills it.
        self.assertIsNone(self.jp.apply_url)
        activity = self._update_activity(
            extension={"apply_url": "https://ats.example/apply/1"},
        )
        result = ingest_update_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_MERGED)
        self.jp.refresh_from_db()
        self.assertEqual(self.jp.apply_url, "https://ats.example/apply/1")

    def test_update_does_not_clobber_non_empty_title(self):
        # JP already carries a title. Update must not overwrite.
        activity = self._update_activity(title="Upstream Renamed Role")
        result = ingest_update_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_MERGED)
        self.jp.refresh_from_db()
        self.assertEqual(self.jp.title, "Original title")

    def test_update_does_not_clobber_non_empty_description(self):
        activity = self._update_activity(content="Upstream rewrote the description.")
        result = ingest_update_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_MERGED)
        self.jp.refresh_from_db()
        self.assertEqual(self.jp.description, "Original description.")

    def test_update_host_mismatch_rejected(self):
        # Different host on the actor → cross-instance update attempt.
        body = self._update_activity(title="ignored")
        body["actor"] = "https://other.example/users/mallory"
        body["object"]["attributedTo"] = "https://other.example/users/mallory"
        result = ingest_update_note(body, federation_activity=_audit_row(body))
        self.assertEqual(result.outcome, OUTCOME_REJECTED)
        self.assertEqual(result.reason, "update_host_mismatch")

    def test_update_unknown_target_skipped(self):
        body = self._update_activity()
        body["object"]["url"] = "https://hire.example/jobs/never-ingested"
        result = ingest_update_note(body, federation_activity=_audit_row(body))
        self.assertEqual(result.outcome, OUTCOME_SKIPPED)
        self.assertEqual(result.reason, "update_target_unknown")


# ---------------------------------------------------------------------------
# Inbound Delete — extends to flip posting_status + complete.
# ---------------------------------------------------------------------------


@override_settings(INSTANCE_ORIGIN=TEST_ORIGIN)
class TestInboundDeleteExtended(TestCase):
    """The Delete handler now flips ``posting_status`` + ``complete`` in
    addition to the existing ``source_deleted_at`` tombstone.

    Cross-instance Deletes still no-op. Replay still preserves the
    first tombstone time. Both behaviors are pinned in 5e + the Phase 4
    delete tests; this module covers the Phase 6b additions only.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="dough6b", password="pw")
        Actor.objects.create(
            preferred_username="dough6b", type="Person", user=self.user,
        )
        self.peer = FakeRemoteActor()
        self.peer_host = self.peer.actor_uri.split("://", 1)[1].split("/", 1)[0]
        # Federated JP whose source_instance matches the signer's host.
        self.jp = JobPost.objects.create(
            title="Federated role",
            link=f"https://{self.peer_host}/jobs/del-1",
            source="federation",
            source_instance=self.peer_host,
            posting_status="open",
            complete=True,
        )
        self.path = "/actors/dough6b/inbox"

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

    def _delete_body(self, object_uri: str) -> bytes:
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{self.peer.actor_uri}/activities/del-1",
            "type": "Delete",
            "actor": self.peer.actor_uri,
            "object": object_uri,
        }
        return json.dumps(activity).encode("utf-8")

    def test_matching_source_delete_flips_posting_status_and_complete(self):
        object_uri = f"{TEST_ORIGIN}/job-posts/{self.jp.id}"
        response = self._post(self._delete_body(object_uri))
        self.assertEqual(response.status_code, 202)
        self.jp.refresh_from_db()
        self.assertIsNotNone(self.jp.source_deleted_at)
        self.assertEqual(self.jp.posting_status, "closed")
        self.assertFalse(self.jp.complete)

    def test_non_matching_source_delete_does_not_change_posting_status(self):
        # Mint a row attributed to a DIFFERENT instance — the peer
        # signing this Delete has no authority over it.
        other = JobPost.objects.create(
            title="Other federation row",
            link="https://other.example/jobs/9",
            source="federation",
            source_instance="other.example",
            posting_status="open",
            complete=True,
        )
        object_uri = f"{TEST_ORIGIN}/job-posts/{other.id}"
        response = self._post(self._delete_body(object_uri))
        self.assertEqual(response.status_code, 202)
        other.refresh_from_db()
        self.assertIsNone(other.source_deleted_at)
        self.assertEqual(other.posting_status, "open")
        self.assertTrue(other.complete)


# ---------------------------------------------------------------------------
# Five-clause visibility (regression — Phase 6b must preserve it).
# ---------------------------------------------------------------------------


class TestVisibilityFilterRegression(TestCase):
    """Federated rows from 6b ingest stay invisible to local users
    until a JobPostDiscovery exists. The 5e visibility tests pin the
    invariant; this module re-verifies under the renamed source value.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="viewer6b", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        activity = _activity(note_url="https://hire.example/jobs/vis-1")
        result = ingest_create_note(activity, federation_activity=_audit_row(activity))
        self.assertEqual(result.outcome, OUTCOME_CREATED)
        self.jp = result.job_post

    def test_federated_row_hidden_without_discovery(self):
        response = self.client.get("/api/v1/job-posts/")
        ids = {item["id"] for item in response.json()["data"]}
        self.assertNotIn(str(self.jp.id), ids)

    def test_federated_row_visible_with_discovery(self):
        JobPostDiscovery.objects.create(
            job_post=self.jp, user=self.user, source="federation",
        )
        response = self.client.get("/api/v1/job-posts/")
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn(str(self.jp.id), ids)


# Silence unused-import lint trigger for the module name (used elsewhere
# in handoff diagnostics + as a monkeypatch target).
_ = federation_ingest
_ = DuplicateAnnotation
_ = FederationFollower
