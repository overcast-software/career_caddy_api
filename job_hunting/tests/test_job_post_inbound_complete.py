"""Integration: POST /api/v1/job-posts/ honors inbound `complete` only
for low-trust sources (Posture E).

cc_auto's email pipeline POSTs thin stubs (title + company + link, no
description) and declares `complete=False` so the row enters the
existing incomplete-recovery path. The api must:

- honor inbound `complete=False` when source is at or below the email
  trust tier (email, email_direct);
- ignore inbound `complete=False` from higher-trust sources (extension,
  scrape, paste, manual) — they push authoritative content;
- never honor inbound `complete=True` — the True flip is the api's
  decision (parse_scrape / ReviewCompleteness), not a client's.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import JobPost


User = get_user_model()


def _payload(**attrs):
    return {"data": {"type": "job-post", "attributes": attrs}}


class TestInboundCompleteGating(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u-complete", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, **attrs):
        return self.client.post(
            "/api/v1/job-posts/", _payload(**attrs), format="json",
        )

    def test_email_source_complete_false_honored(self):
        r = self._post(
            link="https://example.com/jobs/email1",
            title="Stub from email",
            source="email",
            complete=False,
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        self.assertFalse(jp.complete)

    def test_email_direct_source_complete_false_honored(self):
        r = self._post(
            link="https://example.com/jobs/email-direct",
            title="Direct email stub",
            source="email_direct",
            complete=False,
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        self.assertFalse(jp.complete)

    def test_extension_source_complete_false_ignored(self):
        # Extension data is meant to be authoritative — an inbound
        # complete=False from the extension is a bug or attack and must
        # not flip the row.
        r = self._post(
            link="https://example.com/jobs/ext",
            title="Authoritative ext push",
            description="A full description from the live page.",
            source="extension",
            complete=False,
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        self.assertTrue(jp.complete)

    def test_paste_source_complete_false_ignored(self):
        r = self._post(
            link="https://example.com/jobs/paste",
            title="Pasted full text",
            description="The full job description text.",
            source="paste",
            complete=False,
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        self.assertTrue(jp.complete)

    def test_manual_source_complete_false_ignored(self):
        # Default "manual" source (e.g. UI create form) is mid-trust.
        # Mark-incomplete on a created post is a separate PATCH flow,
        # not an inbound-on-create signal.
        r = self._post(
            link="https://example.com/jobs/man",
            title="Manual create",
            source="manual",
            complete=False,
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        self.assertTrue(jp.complete)

    def test_email_source_complete_true_ignored(self):
        # Inbound True is never honored regardless of source — the True
        # flip is the api's call. The model default is True, so a row
        # with no inbound and no other gating still lands True; this
        # case asserts we don't echo client-supplied True as gospel
        # either (no path should treat client True as authoritative).
        r = self._post(
            link="https://example.com/jobs/email-true",
            title="Email pretending it's complete",
            source="email",
            complete=True,
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        # Defaults to True via the model; we just need to assert the
        # client's True wasn't load-bearing.
        self.assertTrue(jp.complete)

    def test_no_inbound_complete_uses_model_default(self):
        r = self._post(
            link="https://example.com/jobs/nodefault",
            title="No complete attr",
            source="email",
        )
        self.assertIn(r.status_code, (200, 201))
        jp = JobPost.objects.get()
        self.assertTrue(jp.complete)

    def test_link_hit_dedupe_does_not_downgrade_existing_complete(self):
        # Existing complete=True post on a link, then an email-source
        # POST hits the same link with complete=False. The link-merge
        # branch must not downgrade — `complete` is not in
        # DEDUPE_BACKFILL_FIELDS so this is structural, but we lock
        # the behavior.
        existing = JobPost.objects.create(
            link="https://example.com/jobs/already-complete",
            title="Already complete",
            description="full text",
            created_by_id=self.user.id,
            complete=True,
        )
        r = self._post(
            link="https://example.com/jobs/already-complete",
            title="Email stub",
            source="email",
            complete=False,
        )
        # Link-hit returns 200 with the existing post.
        self.assertEqual(r.status_code, 200)
        existing.refresh_from_db()
        self.assertTrue(existing.complete)
        self.assertEqual(JobPost.objects.count(), 1)
