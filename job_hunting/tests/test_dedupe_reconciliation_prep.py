"""Stage 5a / 5d of the duplicate-detection reconciliation plan (CC-257).

Covers the prevention→reconciliation prep work that is safe BEFORE the
Stage 2 prod measurement and the Stage 5b `unique=True` drop:

- 5a: the create path survives a same-link write race with 200 against
  the winner instead of a 500 (the IntegrityError handler that later
  becomes Stage 5b's rollback net).
- 5d.1: unlink-duplicate can undo a repost link (previously the one
  irreversible write on the reconciliation surface).
- 5d.2: fingerprints refresh when title/company/location change, on all
  three paths that change them (PATCH, trust-aware overwrite — covered in
  test_job_post_extractor — and the mark verb's field overrides). Stale
  fingerprints used to be a merge hazard; under reconciliation they are a
  recall bug in compute_duplicate_candidates.
- Stage 2 instrumentation: the dedupe-feedback report exposes per-action
  totals, so mark_repost rows are countable and the plan's decisive
  "<20 mark/unlink rows" gate can be read from the endpoint alone.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost
from job_hunting.models.duplicate_annotation import DuplicateAnnotation

User = get_user_model()


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _post_payload(link, title="Engineer", **extra):
    return {
        "data": {
            "type": "job-post",
            "attributes": {
                "title": title,
                "link": link,
                "description": "x" * 500,
                "source": "manual",
                **extra,
            },
        }
    }


class CreateRaceIntegrityErrorTests(TestCase):
    """5a — the only thing between a same-link race and a 500."""

    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")

    def test_race_loser_gets_200_against_the_winner(self):
        """Simulate the race precisely where it happens: after the
        pre-lookup (which saw no row) and before obj.save(). find_duplicate
        is the last call in that window, so its patched side effect plants
        the concurrent winner; save() then hits the unique constraint and
        the handler must re-look-up and return 200 with the winner's id —
        the same contract a sequential repeat-POST gets from the merge
        path."""
        link = "https://example.com/jobs/42"
        winner_holder = {}

        def plant_winner(obj):
            winner_holder["row"] = JobPost.objects.create(
                title="Winner",
                link=link,
                description="y" * 500,
                created_by=self.user,
            )
            return None

        with mock.patch(
            "job_hunting.models.job_post_dedupe.find_duplicate",
            side_effect=plant_winner,
        ):
            resp = _client(self.user).post(
                "/api/v1/job-posts/", _post_payload(link), format="json"
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            resp.json()["data"]["id"], str(winner_holder["row"].id)
        )
        self.assertEqual(JobPost.objects.filter(link=link).count(), 1)


class UnlinkRepostTests(TestCase):
    """5d.1 — repost links become reversible."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="s", password="p", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")
        self.older = JobPost.objects.create(
            title="Role", company=self.company,
            link="https://example.com/a", created_by=self.staff,
        )
        self.newer = JobPost.objects.create(
            title="Role", company=self.company,
            link="https://example.com/b", created_by=self.staff,
        )

    def test_unlink_clears_repost_link_and_annotates(self):
        client = _client(self.staff)
        resp = client.post(
            f"/api/v1/job-posts/{self.newer.id}/mark-duplicate-of/",
            {"target_id": str(self.older.id), "relation": "repost"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.newer.refresh_from_db()
        self.assertEqual(self.newer.reposted_from_id, self.older.id)

        resp = client.post(
            f"/api/v1/job-posts/{self.newer.id}/unlink-duplicate/",
            {}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.newer.refresh_from_db()
        self.assertIsNone(self.newer.reposted_from_id)

        ann = (
            DuplicateAnnotation.objects.filter(action="unlink")
            .order_by("-id").first()
        )
        self.assertIsNotNone(ann)
        self.assertEqual(ann.from_jp_id, self.newer.id)
        self.assertEqual(ann.previous_to_id, self.older.id)
        self.assertEqual(ann.signal_state.get("relation"), "repost")

    def test_duplicate_of_takes_precedence_then_second_unlink_clears_repost(self):
        client = _client(self.staff)
        client.post(
            f"/api/v1/job-posts/{self.newer.id}/mark-duplicate-of/",
            {"target_id": str(self.older.id), "relation": "repost"},
            format="json",
        )
        client.post(
            f"/api/v1/job-posts/{self.newer.id}/mark-duplicate-of/",
            {"target_id": str(self.older.id)},
            format="json",
        )
        self.newer.refresh_from_db()
        self.assertEqual(self.newer.duplicate_of_id, self.older.id)
        self.assertEqual(self.newer.reposted_from_id, self.older.id)

        client.post(
            f"/api/v1/job-posts/{self.newer.id}/unlink-duplicate/",
            {}, format="json",
        )
        self.newer.refresh_from_db()
        self.assertIsNone(self.newer.duplicate_of_id)
        self.assertEqual(self.newer.reposted_from_id, self.older.id)

        client.post(
            f"/api/v1/job-posts/{self.newer.id}/unlink-duplicate/",
            {}, format="json",
        )
        self.newer.refresh_from_db()
        self.assertIsNone(self.newer.reposted_from_id)


class FingerprintRefreshTests(TestCase):
    """5d.2 — fingerprints follow content instead of freezing at creation."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="s", password="p", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")

    def test_patch_title_rederives_fingerprints(self):
        post = JobPost.objects.create(
            title="Old Title", company=self.company,
            link="https://example.com/a", created_by=self.staff,
        )
        before = (post.content_fingerprint, post.normalized_fingerprint)
        self.assertTrue(all(before))
        resp = _client(self.staff).patch(
            f"/api/v1/job-posts/{post.id}/",
            {"data": {"type": "job-post", "id": str(post.id),
                      "attributes": {"title": "New Completely Different Title"}}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        post.refresh_from_db()
        after = (post.content_fingerprint, post.normalized_fingerprint)
        self.assertTrue(all(after))
        self.assertNotEqual(before, after)

    def test_patch_without_identity_fields_keeps_fingerprints(self):
        post = JobPost.objects.create(
            title="Stable Title", company=self.company,
            link="https://example.com/a", created_by=self.staff,
        )
        before = (post.content_fingerprint, post.normalized_fingerprint)
        resp = _client(self.staff).patch(
            f"/api/v1/job-posts/{post.id}/",
            {"data": {"type": "job-post", "id": str(post.id),
                      "attributes": {"description": "z" * 600}}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        post.refresh_from_db()
        self.assertEqual(
            before, (post.content_fingerprint, post.normalized_fingerprint)
        )

    def test_mark_verb_title_override_refreshes_target_fingerprints(self):
        a = JobPost.objects.create(
            title="Better Title From Dupe", company=self.company,
            link="https://example.com/a", created_by=self.staff,
        )
        b = JobPost.objects.create(
            title="Worse Title", company=self.company,
            link="https://example.com/b", created_by=self.staff,
        )
        before = (b.content_fingerprint, b.normalized_fingerprint)
        resp = _client(self.staff).post(
            f"/api/v1/job-posts/{a.id}/mark-duplicate-of/",
            {"target_id": str(b.id), "field_overrides": {"title": "A"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        b.refresh_from_db()
        self.assertEqual(b.title, "Better Title From Dupe")
        after = (b.content_fingerprint, b.normalized_fingerprint)
        self.assertTrue(all(after))
        self.assertNotEqual(before, after)


class DedupeFeedbackActionTotalsTests(TestCase):
    """Stage 2 instrumentation — the gate's denominators, incl. mark_repost."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="s", password="p", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")
        self.a = JobPost.objects.create(
            title="Role", company=self.company,
            link="https://example.com/a", created_by=self.staff,
        )
        self.b = JobPost.objects.create(
            title="Role", company=self.company,
            link="https://example.com/b", created_by=self.staff,
        )

    def test_totals_actions_counts_every_action(self):
        for action in ("mark", "mark_repost", "unlink", "historical"):
            DuplicateAnnotation.objects.create(
                from_jp_id=self.a.id,
                to_jp_id=self.b.id if action != "unlink" else None,
                previous_to_id=self.b.id if action == "unlink" else None,
                action=action,
                signal_state={},
            )
        resp = _client(self.staff).get("/api/v1/reports/dedupe-feedback/")
        self.assertEqual(resp.status_code, 200, resp.content)
        actions = resp.json()["data"]["attributes"]["totals"]["actions"]
        self.assertEqual(actions.get("mark"), 1)
        self.assertEqual(actions.get("mark_repost"), 1)
        self.assertEqual(actions.get("unlink"), 1)
        self.assertEqual(actions.get("historical"), 1)
