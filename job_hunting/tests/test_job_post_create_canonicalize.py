"""POST /api/v1/job-posts/ canonicalize-first dedupe gate.

Regression for the JP 1315 ↔ JP 1550 incident (2026-06-10): the
LinkedIn /comm/jobs/view/ variant of an already-ingested /jobs/view/
post silently minted a duplicate JobPost because the create path
only looked up `link=exact` and never consulted `canonical_link`.

The fix lifts the same canonical-collision gate POST /scrapes/from-text/
already runs (scrapes.py:829-872) into JobPostViewSet.create — but
with one critical asymmetry: a same-link repeat-POST (cc_auto's
email backfill, where the same link is re-POSTed as title/company/
description fill in) must NOT 409. Only canonical-collisions where
the *link itself* differs trip the gate.

Pipeline walk:
- canonical_link: tested here.
- fingerprint: still handled by the downstream find_duplicate block
  (line 739+ in jobs.py); not in scope for this slice.
- sticky-closed: ditto.
- response-shape: 409 with errors[].meta.{job_post_id, title,
  company_name, link} mirrors the from-text envelope so the
  frontend / chat agent / extension treat it the same way.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost, ScrapeProfile
from job_hunting.models.job_post_dedupe import (
    _profile_url_rewrites_for_host,
)


User = get_user_model()


class JobPostCreateCanonicalizeTests(TestCase):
    """LinkedIn /comm/ ↔ /jobs/view/ canonical-collision tests.

    Seeds the ScrapeProfile.url_rewrites entry that canonicalize_link
    relies on to fold /comm/jobs/view/ onto /jobs/view/. Without the
    profile row, both URLs canonicalize unchanged and the gate would
    never see a collision — i.e. the test would silently no-op rather
    than fail loudly. Same setup as
    TestCanonicalizeLinkProfileRewrites in test_job_post_dedupe.py.
    """

    def setUp(self):
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="linkedin.com",
            defaults={"url_rewrites": [{
                "match": r"^https?://www\.linkedin\.com/comm/jobs/view/",
                "rewrite": "https://www.linkedin.com/jobs/view/",
            }]},
        )
        self.alice = User.objects.create_user(username="alice", password="p")
        self.bob = User.objects.create_user(username="bob", password="p")
        self.company = Company.objects.create(name="Acme")

    def tearDown(self):
        _profile_url_rewrites_for_host.cache_clear()

    def _post(self, user, link, source="manual", title="Engineer", **extra):
        client = APIClient()
        client.force_authenticate(user=user)
        attrs = {
            "title": title,
            "link": link,
            "description": "x" * 500,
            "source": source,
            **extra,
        }
        return client.post(
            "/api/v1/job-posts/",
            {"data": {"type": "job-post", "attributes": attrs}},
            format="json",
        )

    def test_canonical_collision_against_complete_row_returns_409(self):
        """The headline regression. JP exists at /jobs/view/N, complete=True.
        New POST arrives at /comm/jobs/view/N — same canonical, different
        link. Same-or-lower trust on the incoming side ⇒ 409, no new row."""
        existing = JobPost.objects.create(
            title="Senior SRE",
            company=self.company,
            link="https://www.linkedin.com/jobs/view/4406564480",
            description="y" * 500,
            created_by=self.alice,
            source="extension",  # high trust on the existing side
            complete=True,
        )
        # Re-canonicalize so the row carries canonical_link (model.save sets
        # this; this explicit refresh just keeps the test independent of any
        # future model-init ordering quirk).
        existing.refresh_from_db()

        before = JobPost.objects.count()
        resp = self._post(
            self.bob,
            "https://www.linkedin.com/comm/jobs/view/4406564480/",
            source="email",  # low trust — same-or-lower than extension
        )
        self.assertEqual(resp.status_code, 409, resp.json())
        self.assertEqual(JobPost.objects.count(), before)  # no new row

        err = resp.json()["errors"][0]
        self.assertEqual(err["code"], "duplicate_job_post")
        meta = err["meta"]
        self.assertEqual(meta["job_post_id"], existing.id)
        self.assertEqual(meta["title"], "Senior SRE")
        self.assertEqual(meta["company_name"], "Acme")
        self.assertEqual(meta["link"], existing.link)

    def test_canonical_collision_against_stub_upgrades_in_place(self):
        """Same canonical match, but existing JP is a stub (complete=False).
        The merge path runs to upgrade the stub. No 409."""
        existing = JobPost.objects.create(
            title="",  # stub — title empty
            link="https://www.linkedin.com/jobs/view/4406564481",
            created_by=self.alice,
            source="email",
            complete=False,
        )
        existing.refresh_from_db()

        before = JobPost.objects.count()
        resp = self._post(
            self.bob,
            "https://www.linkedin.com/comm/jobs/view/4406564481/",
            source="email",
            title="Backend Engineer",
        )
        self.assertIn(resp.status_code, (200, 201), resp.json())
        # No duplicate minted — the stub got upgraded in place.
        self.assertEqual(JobPost.objects.count(), before)

        existing.refresh_from_db()
        self.assertEqual(existing.title, "Backend Engineer")

    def test_higher_trust_source_overrides_409(self):
        """Existing JP is from a low-trust source (email, trust 20). Incoming
        push is high-trust (extension, trust 100). 409 must NOT fire — the
        merge path runs so the higher-trust write can correct the row.
        Same trust-aware override scrapes.py:from_text already does."""
        existing = JobPost.objects.create(
            title="WRONG (email hallucination)",
            company=self.company,
            link="https://www.linkedin.com/jobs/view/4406564482",
            description="y" * 500,
            created_by=self.alice,
            source="email",  # low trust
            complete=True,
        )
        existing.refresh_from_db()

        before = JobPost.objects.count()
        resp = self._post(
            self.bob,
            "https://www.linkedin.com/comm/jobs/view/4406564482/",
            source="extension",  # high trust — should pass through
        )
        # Not a 409: existing is complete but the new write outranks it.
        self.assertNotEqual(resp.status_code, 409, resp.json())
        self.assertEqual(JobPost.objects.count(), before)

    def test_same_link_repeat_post_does_not_409(self):
        """Email-pipeline contract: cc_auto re-POSTs the same link as it
        backfills title / company / description. The current merge path
        (200 with the existing row) must keep working; the new canonical
        gate must NOT fire on link-exact matches.

        JP 1315 ↔ JP 1550 was the *canonical* leak; this test pins the
        link-exact leak we MUST NOT introduce while fixing it."""
        link = "https://www.linkedin.com/jobs/view/4406564483"
        existing = JobPost.objects.create(
            title="",
            link=link,
            created_by=self.alice,
            source="email",
            complete=True,  # complete but bare — exercise the gate condition
        )
        existing.refresh_from_db()

        before = JobPost.objects.count()
        resp = self._post(
            self.bob,
            link,  # IDENTICAL link, same trust tier
            source="email",
            title="Now we know the title",
        )
        # Falls through to the merge path: 200 OK, no duplicate, no 409.
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertEqual(JobPost.objects.count(), before)
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Now we know the title")
