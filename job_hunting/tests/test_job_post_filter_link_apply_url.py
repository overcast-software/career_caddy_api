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

from job_hunting.models import Company, JobPost, ScrapeProfile
from job_hunting.models.job_post_dedupe import _profile_url_rewrites_for_host


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

    def test_long_multibyte_apply_url_inserts_and_matches(self):
        """A near-max multibyte apply_url must insert AND be found.

        Pins the index *choice* (BACK #87): apply_url is
        CharField(max_length=2000), and ``filter[link]`` indexes it via a
        Postgres HASH index, not a btree. A btree index entry must fit
        btree's ~2704-byte per-tuple ceiling; ~1900 three-byte characters
        is ~5700 bytes — it would ERROR on INSERT under a btree index.
        A hash index stores a 32-bit hash, so the row inserts cleanly.

        If anyone ever downgrades jobpost_apply_url_hash_idx to a plain
        btree/db_index, the create() below raises
        ``index row size ... exceeds btree maximum`` and this test fails.
        The lookup matches via the exact-raw ``apply_url=link_filter`` OR
        leg, so it holds regardless of how the value canonicalizes.
        """
        long_apply_url = "https://ats.example.com/apply/" + ("€" * 1900)
        post = JobPost.objects.create(
            title="Long Apply URL Job",
            company=self.company,
            link="https://www.allstate.jobs/jobs/r99999/long-apply",
            apply_url=long_apply_url,
            created_by=self.user,
        )
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": long_apply_url},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(post.id)})

    def test_apply_url_written_with_token_found_by_url_with_other_token(self):
        """CC-139: canonicalize-at-write on the PATCH path.

        A ripplehire-shaped apply_url carries a per-session ``token=`` that
        differs on every visit. Before CC-139 the resolver-captured
        apply_url landed raw, so the user's later landing URL (same job,
        different token) could never equal it and the filter[link] popup
        lookup missed. With a ScrapeProfile url_rewrites rule stripping the
        token for the host, the apply_url is canonicalized AT WRITE (PATCH
        path), so both token variants collapse to the same stored value and
        the lookup matches.
        """
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            # Exact host of the apply URLs below — _rewrite_via_profile
            # matches hostname exactly (www. stripped, no parent-domain
            # suffix walk like the extension-selectors endpoint does).
            hostname="apply.ripplehire.com",
            defaults={"url_rewrites": [{
                "match": r"([?&])token=[^&]*",
                "rewrite": r"\1token=",
            }]},
        )
        self.addCleanup(_profile_url_rewrites_for_host.cache_clear)

        post = JobPost.objects.create(
            title="Ripple Job",
            company=self.company,
            link="https://boards.example.com/jobs/ripple-1",
            created_by=self.user,
        )
        # Extension-style PATCH stamping the apply destination with token A.
        resp = self.client.patch(
            f"/api/v1/job-posts/{post.id}/",
            {
                "data": {
                    "type": "job-post",
                    "id": str(post.id),
                    "attributes": {
                        "apply_url": "https://apply.ripplehire.com/j/42?token=AAA",
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.json())

        post.refresh_from_db()
        # Written value is canonical (token stripped to empty) — NOT raw A.
        self.assertEqual(
            post.apply_url, "https://apply.ripplehire.com/j/42?token="
        )

        # Landing on the same apply URL with a DIFFERENT token must match.
        resp = self.client.get(
            "/api/v1/job-posts/",
            {"filter[link]": "https://apply.ripplehire.com/j/42?token=BBB"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._ids(resp), {str(post.id)})

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
