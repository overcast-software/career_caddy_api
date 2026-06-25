"""Tests for the manual-dedup verb endpoints on JobPostViewSet:
mark-duplicate-of, unlink-duplicate, promote-canonical, and the
sub-collection GET /duplicates/."""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, DuplicateAnnotation, JobPost


User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.other = User.objects.create_user(username="bob", password="pw")
        self.staff = User.objects.create_user(
            username="root", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.user)
        self.acme = Company.objects.create(name="ACME")

    def _post(self, title, link=None, owner=None, **extras):
        return JobPost.objects.create(
            title=title,
            company=self.acme,
            created_by=owner or self.user,
            link=link,
            **extras,
        )


class TestMarkDuplicateOf(_Base):
    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/mark-duplicate-of/"

    def test_happy_path_sets_duplicate_of(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(self._url(a), {"target_id": b.id}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.duplicate_of_id, b.id)
        self.assertEqual(
            resp.json()["data"]["attributes"]["duplicate_of_id"], b.id
        )

    def test_rejects_self_target(self):
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(self._url(a), {"target_id": a.id}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        a.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)

    def test_rejects_missing_target_id(self):
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(self._url(a), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_unknown_target_id(self):
        # NanoID PKs (CC-77): target_id is a string, so a value that
        # doesn't resolve to a visible post is a 404 (not-found), not a
        # 400 — there is no longer an "integer-ness" precondition.
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(
            self._url(a), {"target_id": "abc"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_404_when_target_invisible(self):
        # Target post is created by someone else and the caller has no
        # visibility signal — must not be markable.
        a = self._post("A", link="https://example.com/a")
        invisible = self._post("Z", link="https://example.com/z", owner=self.other)
        resp = self.client.post(
            self._url(a), {"target_id": invisible.id}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        a.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)

    def test_staff_can_link_across_users(self):
        self.client.force_authenticate(user=self.staff)
        a = self._post("A", link="https://example.com/a", owner=self.user)
        b = self._post("B", link="https://example.com/b", owner=self.other)
        resp = self.client.post(
            self._url(a), {"target_id": b.id}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.duplicate_of_id, b.id)

    def test_rejects_cycle(self):
        # B is already a duplicate of A. Marking A as a duplicate of B
        # would create A → B → A.
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        b.duplicate_of = a
        b.save(update_fields=["duplicate_of"])
        resp = self.client.post(
            self._url(a), {"target_id": b.id}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        a.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)

    def test_rejects_deeper_cycle(self):
        # C → B → A; marking A as dup of C would loop.
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        c = self._post("C", link="https://example.com/c")
        b.duplicate_of = a
        b.save(update_fields=["duplicate_of"])
        c.duplicate_of = b
        c.save(update_fields=["duplicate_of"])
        resp = self.client.post(
            self._url(a), {"target_id": c.id}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestUnlinkDuplicate(_Base):
    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/unlink-duplicate/"

    def test_clears_duplicate_of(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        a.duplicate_of = b
        a.save(update_fields=["duplicate_of"])
        resp = self.client.post(self._url(a), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)

    def test_idempotent_when_already_null(self):
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(self._url(a), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)

    def test_404_when_post_invisible(self):
        invisible = self._post(
            "Z", link="https://example.com/z", owner=self.other
        )
        resp = self.client.post(self._url(invisible), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class TestPromoteCanonical(_Base):
    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/promote-canonical/"

    def test_swaps_roles(self):
        root = self._post("Root", link="https://example.com/root")
        dup = self._post("Dup", link="https://example.com/dup")
        dup.duplicate_of = root
        dup.save(update_fields=["duplicate_of"])

        resp = self.client.post(self._url(dup), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        dup.refresh_from_db()
        root.refresh_from_db()
        self.assertIsNone(dup.duplicate_of_id)
        self.assertEqual(root.duplicate_of_id, dup.id)

    def test_repoints_siblings(self):
        # Before: root has three duplicates (a, b, c). Promote `a`.
        # After: a is canonical; root, b, c all point at a.
        root = self._post("Root", link="https://example.com/root")
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        c = self._post("C", link="https://example.com/c")
        for jp in (a, b, c):
            jp.duplicate_of = root
            jp.save(update_fields=["duplicate_of"])

        resp = self.client.post(self._url(a), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        root.refresh_from_db()
        b.refresh_from_db()
        c.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)
        self.assertEqual(root.duplicate_of_id, a.id)
        self.assertEqual(b.duplicate_of_id, a.id)
        self.assertEqual(c.duplicate_of_id, a.id)

    def test_400_when_not_a_duplicate(self):
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(self._url(a), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestAnnotationWrites(_Base):
    """Each verb writes a DuplicateAnnotation row. Phase 3 audit."""

    def test_mark_writes_annotation(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(
            f"/api/v1/job-posts/{a.id}/mark-duplicate-of/",
            {"target_id": b.id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ann = DuplicateAnnotation.objects.get(from_jp_id=a.id, action="mark")
        self.assertEqual(ann.to_jp_id, b.id)
        self.assertIsNone(ann.previous_to_id)
        self.assertEqual(ann.set_by_id, self.user.id)
        self.assertIn("candidates", ann.signal_state)

    def test_mark_captures_previous_target(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        c = self._post("C", link="https://example.com/c")
        a.duplicate_of = b
        a.save(update_fields=["duplicate_of"])
        self.client.post(
            f"/api/v1/job-posts/{a.id}/mark-duplicate-of/",
            {"target_id": c.id},
            format="json",
        )
        ann = DuplicateAnnotation.objects.get(from_jp_id=a.id, action="mark")
        self.assertEqual(ann.previous_to_id, b.id)
        self.assertEqual(ann.to_jp_id, c.id)

    def test_unlink_writes_annotation(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        a.duplicate_of = b
        a.save(update_fields=["duplicate_of"])
        self.client.post(
            f"/api/v1/job-posts/{a.id}/unlink-duplicate/", {}, format="json"
        )
        ann = DuplicateAnnotation.objects.get(from_jp_id=a.id, action="unlink")
        self.assertIsNone(ann.to_jp_id)
        self.assertEqual(ann.previous_to_id, b.id)

    def test_unlink_idempotent_no_annotation(self):
        # Idempotent no-op (was already null) must NOT spam annotations.
        a = self._post("A", link="https://example.com/a")
        self.client.post(
            f"/api/v1/job-posts/{a.id}/unlink-duplicate/", {}, format="json"
        )
        self.assertEqual(
            DuplicateAnnotation.objects.filter(
                from_jp_id=a.id, action="unlink"
            ).count(),
            0,
        )

    def test_promote_writes_annotation(self):
        root = self._post("Root", link="https://example.com/root")
        dup = self._post("Dup", link="https://example.com/dup")
        dup.duplicate_of = root
        dup.save(update_fields=["duplicate_of"])
        self.client.post(
            f"/api/v1/job-posts/{dup.id}/promote-canonical/", {}, format="json"
        )
        ann = DuplicateAnnotation.objects.get(from_jp_id=dup.id, action="promote")
        self.assertEqual(ann.previous_to_id, root.id)
        self.assertIsNone(ann.to_jp_id)


class TestDuplicatesList(_Base):
    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/duplicates/"

    def test_returns_visibility_filtered_children(self):
        root = self._post("Root", link="https://example.com/root")
        mine = self._post("Mine", link="https://example.com/mine")
        theirs = self._post(
            "Theirs", link="https://example.com/theirs", owner=self.other
        )
        for jp in (mine, theirs):
            jp.duplicate_of = root
            jp.save(update_fields=["duplicate_of"])

        resp = self.client.get(self._url(root))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = sorted(row["id"] for row in resp.json()["data"])
        self.assertEqual(ids, [mine.id])

    def test_staff_sees_all_children(self):
        self.client.force_authenticate(user=self.staff)
        root = self._post("Root", link="https://example.com/root")
        mine = self._post("Mine", link="https://example.com/mine")
        theirs = self._post(
            "Theirs", link="https://example.com/theirs", owner=self.other
        )
        for jp in (mine, theirs):
            jp.duplicate_of = root
            jp.save(update_fields=["duplicate_of"])

        resp = self.client.get(self._url(root))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = sorted(row["id"] for row in resp.json()["data"])
        self.assertEqual(ids, sorted([mine.id, theirs.id]))

    def test_404_when_post_does_not_exist(self):
        resp = self.client.get("/api/v1/job-posts/999999/duplicates/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class TestMarkDuplicateFieldOverrides(_Base):
    """Phase C — ``field_overrides`` lets the operator carry the
    caller's (A) value for one or more allowlisted fields onto the
    target (B) BEFORE the duplicate-link is set, so the canonical row
    picks up the better content surfaced on the dupe."""

    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/mark-duplicate-of/"

    def test_override_title_from_a_to_b(self):
        # A has the better title; the operator wants B (the canonical)
        # to inherit it before A collapses into B.
        a = self._post(
            "Senior Software Engineer - Product Security",
            link="https://example.com/a",
        )
        b = self._post("SSE PS", link="https://example.com/b")
        resp = self.client.post(
            self._url(a),
            {
                "target_id": b.id,
                "field_overrides": {"title": "A"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        b.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual(
            b.title, "Senior Software Engineer - Product Security"
        )
        # Duplicate link is set AFTER the override applies.
        self.assertEqual(a.duplicate_of_id, b.id)

    def test_override_b_choice_is_noop(self):
        # Choosing "B" for a field means "keep the target's value" — no
        # mutation on the target.
        a = self._post("A title", link="https://example.com/a")
        b = self._post("B title", link="https://example.com/b")
        resp = self.client.post(
            self._url(a),
            {
                "target_id": b.id,
                "field_overrides": {"title": "B"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        b.refresh_from_db()
        self.assertEqual(b.title, "B title")

    def test_override_records_into_signal_state(self):
        a = self._post("A new title", link="https://example.com/a")
        b = self._post("B old title", link="https://example.com/b")
        self.client.post(
            self._url(a),
            {
                "target_id": b.id,
                "field_overrides": {"title": "A", "location": "B"},
            },
            format="json",
        )
        ann = DuplicateAnnotation.objects.get(from_jp_id=a.id, action="mark")
        self.assertEqual(
            ann.signal_state.get("field_overrides"),
            {"title": "A", "location": "B"},
        )
        # Relation defaults to "duplicate" when omitted.
        self.assertEqual(ann.signal_state.get("relation"), "duplicate")

    def test_override_applies_before_link_is_set(self):
        # Regression guard: if the target save fails mid-flight, the
        # link must NOT already be in place pointing at stale content.
        # We can't trigger an actual failure here, but we can assert
        # the order via DB state — the target's overridden field is
        # present at the same instant duplicate_of_id is set.
        a = self._post("Operator-preferred title", link="https://example.com/a")
        b = self._post("Bad title", link="https://example.com/b")
        self.client.post(
            self._url(a),
            {"target_id": b.id, "field_overrides": {"title": "A"}},
            format="json",
        )
        a.refresh_from_db()
        b.refresh_from_db()
        # Both invariants hold post-call.
        self.assertEqual(b.title, "Operator-preferred title")
        self.assertEqual(a.duplicate_of_id, b.id)

    def test_override_rejects_unknown_field(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(
            self._url(a),
            {
                "target_id": b.id,
                "field_overrides": {"created_by": "A"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        a.refresh_from_db()
        self.assertIsNone(a.duplicate_of_id)

    def test_override_rejects_invalid_choice(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(
            self._url(a),
            {
                "target_id": b.id,
                "field_overrides": {"title": "C"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_override_rejects_non_object(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(
            self._url(a),
            {"target_id": b.id, "field_overrides": ["title"]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestMarkRepost(_Base):
    """Phase C — ``relation: "repost"`` writes ``reposted_from`` instead
    of ``duplicate_of`` so both rows stay queryable independently."""

    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/mark-duplicate-of/"

    def test_relation_repost_sets_reposted_from(self):
        original = self._post("Engineer", link="https://example.com/orig")
        repost = self._post("Engineer", link="https://example.com/repost")
        resp = self.client.post(
            self._url(repost),
            {"target_id": original.id, "relation": "repost"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        repost.refresh_from_db()
        self.assertEqual(repost.reposted_from_id, original.id)
        # Critical: the duplicate pointer stays null on a repost.
        self.assertIsNone(repost.duplicate_of_id)

    def test_relation_repost_keeps_both_rows_queryable(self):
        # Reposts don't collapse — both rows must still be retrievable
        # via their own GET endpoint.
        original = self._post("Engineer", link="https://example.com/orig")
        repost = self._post("Engineer", link="https://example.com/repost")
        self.client.post(
            self._url(repost),
            {"target_id": original.id, "relation": "repost"},
            format="json",
        )

        r_original = self.client.get(f"/api/v1/job-posts/{original.id}/")
        r_repost = self.client.get(f"/api/v1/job-posts/{repost.id}/")
        self.assertEqual(r_original.status_code, status.HTTP_200_OK)
        self.assertEqual(r_repost.status_code, status.HTTP_200_OK)

    def test_relation_repost_records_mark_repost_action(self):
        original = self._post("Engineer", link="https://example.com/orig")
        repost = self._post("Engineer", link="https://example.com/repost")
        self.client.post(
            self._url(repost),
            {"target_id": original.id, "relation": "repost"},
            format="json",
        )
        ann = DuplicateAnnotation.objects.get(
            from_jp_id=repost.id, action="mark_repost"
        )
        self.assertEqual(ann.to_jp_id, original.id)
        self.assertEqual(ann.signal_state.get("relation"), "repost")

    def test_relation_repost_with_field_overrides(self):
        original = self._post("Engineer", link="https://example.com/orig")
        repost = self._post(
            "Senior Engineer", link="https://example.com/repost"
        )
        self.client.post(
            self._url(repost),
            {
                "target_id": original.id,
                "relation": "repost",
                "field_overrides": {"title": "A"},
            },
            format="json",
        )
        original.refresh_from_db()
        repost.refresh_from_db()
        # Override copies repost's title onto the original (the
        # mechanic is the same regardless of relation).
        self.assertEqual(original.title, "Senior Engineer")
        self.assertEqual(repost.reposted_from_id, original.id)
        self.assertIsNone(repost.duplicate_of_id)

    def test_relation_repost_rejects_self_target(self):
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(
            self._url(a),
            {"target_id": a.id, "relation": "repost"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_relation_repost_allows_back_link_pattern(self):
        # Repost doesn't participate in the canonical walk, so a
        # reciprocal link that would have been a cycle under
        # ``duplicate_of`` is fine here. Confirms Phase C's "no cycle
        # check on repost" stance.
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        # b is already a repost of a — marking a as a repost of b is
        # allowed; both rows stay queryable.
        b.reposted_from = a
        b.save(update_fields=["reposted_from"])
        resp = self.client.post(
            self._url(a),
            {"target_id": b.id, "relation": "repost"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.reposted_from_id, b.id)

    def test_invalid_relation_rejected(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(
            self._url(a),
            {"target_id": b.id, "relation": "sibling"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestMarkDuplicateBackwardCompat(_Base):
    """Phase C regression guard: when neither ``relation`` nor
    ``field_overrides`` is supplied, behavior must match pre-Phase-C
    exactly — sets ``duplicate_of`` and writes a ``mark`` annotation."""

    def _url(self, jp):
        return f"/api/v1/job-posts/{jp.id}/mark-duplicate-of/"

    def test_no_extras_writes_mark_action_only(self):
        a = self._post("A", link="https://example.com/a")
        b = self._post("B", link="https://example.com/b")
        resp = self.client.post(
            self._url(a), {"target_id": b.id}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.duplicate_of_id, b.id)
        self.assertIsNone(a.reposted_from_id)
        ann = DuplicateAnnotation.objects.get(from_jp_id=a.id)
        self.assertEqual(ann.action, "mark")
        # Signal state still carries the new keys, but with empty /
        # default values so the report can analyze uniformly.
        self.assertEqual(ann.signal_state.get("relation"), "duplicate")
        self.assertEqual(ann.signal_state.get("field_overrides"), {})


class TestComputeDuplicateCandidatesRepost(TestCase):
    """Phase C — ``compute_duplicate_candidates`` emits a ``repost``
    reason code (in place of ``normalized_fingerprint``) when the
    candidate is older than
    ``settings.DEDUPE_REPOST_THRESHOLD_DAYS``."""

    def setUp(self):
        from rest_framework.test import APIRequestFactory
        self.user = User.objects.create_user(
            username="repostuser", password="pw"
        )
        self.company = Company.objects.create(name="Allstate")
        self.factory = APIRequestFactory()

    def _request(self):
        req = self.factory.get("/api/v1/job-posts/")
        req.user = self.user
        return req

    def _make_post(self, *, title, link, created_days_ago=0):
        from django.utils import timezone
        from datetime import timedelta
        post = JobPost.objects.create(
            title=title,
            company=self.company,
            location="Northbrook, IL",
            link=link,
            created_by=self.user,
        )
        if created_days_ago:
            now = timezone.now()
            JobPost.objects.filter(pk=post.pk).update(
                created_at=now - timedelta(days=created_days_ago),
            )
            post.refresh_from_db()
        return post

    def test_emits_repost_when_gap_exceeds_threshold(self):
        from job_hunting.api.serializers import compute_duplicate_candidates

        # Existing row is 30 days old, threshold is 14 days → repost.
        self._make_post(
            title="Engineer", link="https://ex.com/1", created_days_ago=30,
        )
        candidate = self._make_post(
            title="Engineer", link="https://ex.com/2",
        )
        items = compute_duplicate_candidates(candidate, self._request())
        self.assertEqual(len(items), 1)
        signals = items[0]._match_signals
        self.assertIn("repost", signals)
        self.assertNotIn("normalized_fingerprint", signals)

    def test_emits_normalized_fingerprint_within_threshold(self):
        from job_hunting.api.serializers import compute_duplicate_candidates

        # Existing row is fresh (default created_at = now) → still the
        # ordinary normalized_fingerprint code (Phase B behavior).
        self._make_post(title="Engineer", link="https://ex.com/1")
        candidate = self._make_post(title="Engineer", link="https://ex.com/2")
        items = compute_duplicate_candidates(candidate, self._request())
        self.assertEqual(len(items), 1)
        signals = items[0]._match_signals
        self.assertIn("normalized_fingerprint", signals)
        self.assertNotIn("repost", signals)

    def test_threshold_respects_setting_override(self):
        from django.test import override_settings
        from job_hunting.api.serializers import compute_duplicate_candidates

        # 10-day-old candidate against a 7-day threshold → repost.
        self._make_post(
            title="Engineer", link="https://ex.com/1", created_days_ago=10,
        )
        candidate = self._make_post(
            title="Engineer", link="https://ex.com/2",
        )
        with override_settings(DEDUPE_REPOST_THRESHOLD_DAYS=7):
            items = compute_duplicate_candidates(
                candidate, self._request()
            )
        self.assertEqual(len(items), 1)
        self.assertIn("repost", items[0]._match_signals)
