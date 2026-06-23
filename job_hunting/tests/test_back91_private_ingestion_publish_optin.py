"""BACK-91 — ingestion is private; publishing is a per-user opt-in.

Asserts the decoupling of ingestion from publishing:

- A bare ``JobPost`` is born private (``audience == []``).
- ``audience_for_user`` resolves the per-user ``Profile.federate_posts``
  opt-in (default OFF) — no per-post publish flag.
- Ingesting a job (the extractor persist path AND the JobPost POST path)
  for a NON-opted-in user lands private and fans out NOTHING; for an
  opted-in user it lands public and fans out a Create.
- Existing public posts are left as-is.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, Profile, Scrape  # noqa: F401
from job_hunting.models.job_post import AS2_PUBLIC, audience_for_user
from job_hunting.lib.parsers.job_post_extractor import (
    JobPostExtractor,
    ParsedJobData,
)

User = get_user_model()

# The signal handler enqueues via this symbol; patching it lets us assert
# the fanout decision without exercising dispatch internals.
ENQUEUE = "job_hunting.signals.federation.enqueue_jobpost_activity"


class TestAudienceDefaultAndHelper(TestCase):
    """Model default + the opt-in resolver."""

    def test_bare_jobpost_is_private(self):
        # The whole point of BACK-91: a fresh row defaults to private.
        self.assertEqual(JobPost().audience, [])

    def test_audience_for_user_none(self):
        self.assertEqual(audience_for_user(None), [])

    def test_audience_for_user_no_profile(self):
        user = User.objects.create_user(username="noprof", password="pass")
        self.assertEqual(audience_for_user(user), [])

    def test_audience_for_user_opted_out(self):
        user = User.objects.create_user(username="optout", password="pass")
        Profile.objects.create(user=user, federate_posts=False)
        self.assertEqual(audience_for_user(user), [])

    def test_audience_for_user_opted_in(self):
        user = User.objects.create_user(username="optin", password="pass")
        Profile.objects.create(user=user, federate_posts=True)
        self.assertEqual(audience_for_user(user), [AS2_PUBLIC])


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestExtractorIngestionOptIn(TestCase):
    """The extractor persist path (JobPostExtractor.process_evaluation)."""

    def _parsed(self):
        return ParsedJobData(
            title="Senior Engineer",
            company_name="Acme Corp",
            company_display_name="Acme",
            description="Build things.",
            location="Remote",
            remote=True,
        )

    def test_ingest_not_opted_in_is_private_and_no_dispatch(self):
        user = User.objects.create_user(username="ingest_priv", password="pass")
        scrape = Scrape.objects.create(
            url="https://example.com/job/priv",
            status="extracting",
            created_by=user,
        )
        with patch(ENQUEUE) as enq:
            JobPostExtractor().process_evaluation(scrape, self._parsed(), user=user)
        job = JobPost.objects.get(link="https://example.com/job/priv")
        self.assertEqual(job.audience, [])
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertNotIn("create", kinds)

    def test_ingest_opted_in_is_public_and_dispatches(self):
        user = User.objects.create_user(username="ingest_pub", password="pass")
        Profile.objects.create(user=user, federate_posts=True)
        scrape = Scrape.objects.create(
            url="https://example.com/job/pub",
            status="extracting",
            created_by=user,
        )
        with patch(ENQUEUE) as enq:
            JobPostExtractor().process_evaluation(scrape, self._parsed(), user=user)
        job = JobPost.objects.get(link="https://example.com/job/pub")
        self.assertEqual(job.audience, [AS2_PUBLIC])
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertIn("create", kinds)


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestViewsetCreateOptIn(TestCase):
    """The JobPost POST path (JobPostViewSet.create)."""

    def _post(self, client):
        payload = {
            "data": {
                "type": "job-post",
                "attributes": {
                    "title": "QA Engineer",
                    "description": "Test everything",
                    "link": "https://example.com/jobs/qa",
                },
            }
        }
        return client.post("/api/v1/job-posts/", data=payload, format="json")

    def test_create_not_opted_in_is_private_and_no_dispatch(self):
        user = User.objects.create_user(username="view_priv", password="pass")
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(ENQUEUE) as enq:
            resp = self._post(client)
        self.assertIn(resp.status_code, (200, 201))
        job = JobPost.objects.get(link="https://example.com/jobs/qa")
        self.assertEqual(job.audience, [])
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertNotIn("create", kinds)

    def test_create_opted_in_is_public_and_dispatches(self):
        user = User.objects.create_user(username="view_pub", password="pass")
        Profile.objects.create(user=user, federate_posts=True)
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(ENQUEUE) as enq:
            resp = self._post(client)
        self.assertIn(resp.status_code, (200, 201))
        job = JobPost.objects.get(link="https://example.com/jobs/qa")
        self.assertEqual(job.audience, [AS2_PUBLIC])
        kinds = [c.args[1] for c in enq.call_args_list]
        self.assertIn("create", kinds)


@override_settings(INSTANCE_ORIGIN="http://testserver")
class TestExistingPublicLeftAsIs(TestCase):
    """Existing already-public posts must not be retro-flipped (Doug's call)."""

    def test_existing_public_post_stays_public(self):
        user = User.objects.create_user(username="legacy_pub", password="pass")
        post = JobPost.objects.create(
            created_by=user,
            title="Legacy",
            description="x",
            link="https://example.com/jobs/legacy",
            audience=[AS2_PUBLIC],
        )
        post.refresh_from_db()
        self.assertEqual(post.audience, [AS2_PUBLIC])
