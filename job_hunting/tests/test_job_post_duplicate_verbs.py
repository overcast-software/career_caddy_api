"""Tests for the manual-dedup verb endpoints on JobPostViewSet:
mark-duplicate-of, unlink-duplicate, promote-canonical, and the
sub-collection GET /duplicates/."""

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost


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

    def test_rejects_non_integer_target_id(self):
        a = self._post("A", link="https://example.com/a")
        resp = self.client.post(
            self._url(a), {"target_id": "abc"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

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
        ids = sorted(int(row["id"]) for row in resp.json()["data"])
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
        ids = sorted(int(row["id"]) for row in resp.json()["data"])
        self.assertEqual(ids, sorted([mine.id, theirs.id]))

    def test_404_when_post_does_not_exist(self):
        resp = self.client.get("/api/v1/job-posts/999999/duplicates/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
