"""Regression test: filter[link]= must also match against apply_url.

The browser extension's popup-open lookup ("is this URL already in my
library?") sends the raw tab URL. JobPosts carry up to three URL
fields that can represent the same job:

- ``link``           — the listing URL (jobboard or ATS landing)
- ``canonical_link`` — the canonicalized form of ``link`` (tracking
                       params stripped, host-specific rewrites applied)
- ``apply_url``      — the apply destination, often a different host
                       than ``link`` (e.g. company jobs page links out
                       to its ATS at workday/greenhouse/contacthr)

Reproducer: JP 1329 (Allstate, 2026-06-09). The JP was created via
the popup against the Allstate listing page; the ``apply_url``
``allstateinsurance.contacthr.com/151916501`` was stored as the apply
destination. Later, navigating directly to the contacthr ATS URL,
the popup-open lookup against ``filter[link]=...contacthr.com/151916501``
returned ``data: []`` — the URL only matched ``apply_url``, not
``link`` or ``canonical_link``, and the old query missed it.

After the fix, ``filter[link]=<url>`` also matches
``apply_url == url`` and ``apply_url == canonicalize_link(url)``.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company, JobPost


User = get_user_model()


class TestJobPostFilterLinkMatchesApplyUrl(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="searcher", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Allstate")
        # JP 1329 shape: listing on allstate's careers site, apply
        # destination on the contacthr ATS host.
        self.post = JobPost.objects.create(
            title="Claims Adjuster",
            company=self.company,
            link="https://www.allstate.jobs/jobs/r12345/claims-adjuster",
            apply_url="https://allstateinsurance.contacthr.com/151916501",
            created_by=self.user,
        )

    def _ids(self, response):
        return {row["id"] for row in response.json()["data"]}

    def test_lookup_by_apply_url_matches(self):
        """Hitting the stored apply destination directly must return the JP."""
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://allstateinsurance.contacthr.com/151916501"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(self.post.id)})

    def test_lookup_by_apply_url_with_tracking_matches_via_canonical(self):
        """User on the apply URL with utm tracking should still match.

        The query value is canonicalized; the OR-leg
        ``apply_url=canonical`` catches the case where the stored
        apply_url is the clean canonical form (the more common shape —
        ATS URLs rarely come with tracking attached).
        """
        resp = self.client.get(
            "/api/v1/job-posts/",
            {
                "filter[link]": (
                    "https://allstateinsurance.contacthr.com/151916501"
                    "?utm_source=newsletter"
                )
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(self.post.id)})

    def test_lookup_by_link_still_matches(self):
        """Regression: the original listing URL match still works."""
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://www.allstate.jobs/jobs/r12345/claims-adjuster"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(self.post.id)})

    def test_unrelated_url_does_not_match(self):
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://example.com/some/other/job"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), set())

    def test_null_apply_url_does_not_match_empty_query(self):
        """Edge: a JP with apply_url=NULL must not match an empty filter.

        SQL ``=`` never matches NULL, so this is the SQL default — the
        test pins that we did not introduce a stray ``OR apply_url=''``
        path that would false-match every NULL row.
        """
        # Sibling JP with no apply_url; only link is set.
        sibling = JobPost.objects.create(
            title="No Apply URL Job",
            company=self.company,
            link="https://example.com/job/no-apply",
            created_by=self.user,
        )
        # Sanity: ORM stores NULL when omitted (CharField null=True).
        sibling.refresh_from_db()
        self.assertIsNone(sibling.apply_url)

        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": ""},
        )
        self.assertEqual(resp.status_code, 200)
        # Empty filter[link] short-circuits the per-user qs branch (it's
        # not None), so the lookup runs but matches nothing — neither
        # the NULL-apply_url sibling nor the populated allstate row.
        self.assertEqual(self._ids(resp), set())
