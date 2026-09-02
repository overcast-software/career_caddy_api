"""BACK-130 — ``compute_duplicate_candidates`` visibility.

The function used to inline a five-clause copy of the job-post
visibility predicate (created / applied / scored / scraped / discovered)
that had lost ``Q(user_memberships__user_id=...)``. A post reachable only
through ``UserJobPost`` was therefore never offered as a duplicate
candidate, while ``JobPostViewSet._visible_jobpost_qs`` — the authz gate
on the mark/unlink/promote verbs — already delegated to the six-clause
``JobPost.objects.visible_to``. The gate and the candidate computation
disagreed: a user could mark a post duplicate-of a row the dedupe UI
would never propose.

These tests pin the widened candidate set (the intended behaviour change)
*and* the edges the swap must not move: posts with no per-user signal at
all stay invisible, a membership belonging to somebody else grants
nothing, staff still see across users, an anonymous caller still gets
``[]``, and the self / duplicate_of exclusion set still bites.

They also pin the other half of "the gate and the candidate set must
agree": the SUBJECT post is resolved through the same gate, so
``/duplicate-candidates/`` 404s wherever ``GET /job-posts/<id>/`` does
instead of answering 200 with an oracle on a post you cannot read.

The report call site of the same predicate (``views/reports.py``, the
other five-clause copy) is BACK-129's, fixed on its own branch — not
duplicated here.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory

from job_hunting.api.serializers import compute_duplicate_candidates
from job_hunting.models import Company, JobPost, UserJobPost

User = get_user_model()


class TestDuplicateCandidatesMemberVisibility(TestCase):
    """A post whose ONLY link to the caller is a ``UserJobPost`` row."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(username="member", password="pw")
        self.author = User.objects.create_user(username="author", password="pw")
        self.company = Company.objects.create(name="SNBL USA")

    def _request(self, user=None):
        req = self.factory.get("/api/v1/job-posts/")
        req.user = self.user if user is None else user
        return req

    def _post(self, link, *, created_by=None, duplicate_of=None):
        """Same title/company/location on every row so the fingerprint
        columns collide and the dedupe signals fire; ``link`` is unique
        so the rows are distinct."""
        return JobPost.objects.create(
            title="Engineer",
            company=self.company,
            location="Northbrook, IL",
            link=link,
            created_by=self.author if created_by is None else created_by,
            duplicate_of=duplicate_of,
        )

    def _member_post(self, link, **kwargs):
        """A post authored by someone else, reachable by ``self.user``
        through membership and nothing else."""
        jp = self._post(link, **kwargs)
        UserJobPost.objects.create(job_post=jp, user=self.user)
        return jp

    # --- the BACK-130 regression ------------------------------------

    def test_member_only_post_is_offered_as_candidate(self):
        """The failing case. ``other`` shares a fingerprint with the
        caller's post and is reachable only via ``user_memberships`` —
        under the five-clause filter it was silently absent."""
        mine = self._post("https://ex.com/mine", created_by=self.user)
        other = self._member_post("https://ex.com/theirs")

        items = compute_duplicate_candidates(mine, self._request())

        self.assertEqual([it.id for it in items], [other.id])

    def test_member_only_post_surfaces_through_the_endpoint(self):
        """Same case across the HTTP boundary, so the fix reaches the
        payload the dedupe UI actually reads and not just the helper."""
        mine = self._post("https://ex.com/mine", created_by=self.user)
        other = self._member_post("https://ex.com/theirs")

        client = APIClient()
        client.force_authenticate(user=self.user)
        resp = client.get(f"/api/v1/job-posts/{mine.id}/duplicate-candidates/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["id"] for row in resp.json()["data"]], [str(other.id)]
        )

    def test_authz_gate_and_candidate_set_now_agree(self):
        """The disagreement the ticket names: the member-visible row is
        reachable by the verb gate, so it must also be proposable."""
        from job_hunting.api.views.jobs import JobPostViewSet

        mine = self._post("https://ex.com/mine", created_by=self.user)
        other = self._member_post("https://ex.com/theirs")

        gated = JobPostViewSet._visible_jobpost_qs(self._request())
        self.assertIn(other.id, set(gated.values_list("id", flat=True)))

        items = compute_duplicate_candidates(mine, self._request())
        self.assertIn(other.id, {it.id for it in items})

    # --- the edges the swap must NOT move ---------------------------

    def test_post_with_no_user_signal_is_still_invisible(self):
        """Exonerating case: the fix widens by exactly the membership
        clause, and by MY membership specifically.

        Two fingerprint twins must stay out. ``bare`` has no per-user row
        of any kind. ``theirs`` carries a ``UserJobPost`` owned by someone
        else — so a regression from
        ``Q(user_memberships__user_id=me)`` to
        ``Q(user_memberships__isnull=False)`` (any membership, not mine)
        would leak every member-owned post on the platform, and only this
        row catches it."""
        mine = self._post("https://ex.com/mine", created_by=self.user)
        self._post("https://ex.com/bare")  # authored by self.author, no rows
        theirs = self._post("https://ex.com/stranger")
        UserJobPost.objects.create(job_post=theirs, user=self.author)

        items = compute_duplicate_candidates(mine, self._request())

        self.assertEqual(items, [])

    def test_staff_still_see_candidates_across_users(self):
        staff = User.objects.create_user(
            username="staffer", password="pw", is_staff=True
        )
        mine = self._post("https://ex.com/mine", created_by=self.user)
        stranger = self._post("https://ex.com/stranger")

        items = compute_duplicate_candidates(mine, self._request(user=staff))

        self.assertEqual({it.id for it in items}, {stranger.id})

    def test_anonymous_user_returns_empty(self):
        mine = self._post("https://ex.com/mine", created_by=self.user)
        self._member_post("https://ex.com/theirs")

        items = compute_duplicate_candidates(
            mine, self._request(user=AnonymousUser())
        )

        self.assertEqual(items, [])

    def test_missing_request_returns_empty(self):
        mine = self._post("https://ex.com/mine", created_by=self.user)
        self._member_post("https://ex.com/theirs")

        self.assertEqual(compute_duplicate_candidates(mine, None), [])

    def test_subject_post_the_caller_cannot_see_is_404(self):
        """The mirror of the ticket's thesis. ``duplicate_candidates``
        used to resolve its SUBJECT with an unscoped
        ``JobPost.objects.filter(pk=pk)``, unlike every other dedupe verb.
        The rows it returns are all the caller's own, so nothing foreign
        was disclosed directly — but empty-vs-non-empty is an oracle on
        the subject's canonical_link / fingerprints / apply_url /
        company+title. It now 404s exactly like ``GET /job-posts/<id>/``."""
        stranger = self._post("https://ex.com/stranger")
        self._post("https://ex.com/mine", created_by=self.user)

        client = APIClient()
        client.force_authenticate(user=self.user)

        detail = client.get(f"/api/v1/job-posts/{stranger.id}/")
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)

        resp = client.get(
            f"/api/v1/job-posts/{stranger.id}/duplicate-candidates/"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_member_visible_subject_post_is_still_reachable(self):
        """...and the gate must not close on the row this ticket widened
        access to: a subject the caller reaches only by membership still
        answers 200."""
        subject = self._member_post("https://ex.com/theirs")
        mine = self._post("https://ex.com/mine", created_by=self.user)

        client = APIClient()
        client.force_authenticate(user=self.user)
        resp = client.get(
            f"/api/v1/job-posts/{subject.id}/duplicate-candidates/"
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["id"] for row in resp.json()["data"]], [str(mine.id)]
        )

    def test_exclusion_set_still_applies_to_member_visible_rows(self):
        """The widened candidate set must not smuggle the settled
        duplicate_of chain back in. All three twins below are
        member-visible; only the unrelated one may surface."""
        parent = self._member_post("https://ex.com/parent")
        mine = self._post(
            "https://ex.com/mine", created_by=self.user, duplicate_of=parent
        )
        child = self._member_post("https://ex.com/child", duplicate_of=mine)
        unrelated = self._member_post("https://ex.com/unrelated")

        items = compute_duplicate_candidates(mine, self._request())
        ids = {it.id for it in items}

        self.assertEqual(ids, {unrelated.id})
        self.assertNotIn(mine.id, ids)  # self
        self.assertNotIn(parent.id, ids)  # duplicate_of target
        self.assertNotIn(child.id, ids)  # points back at mine
