"""management command: recanonicalize_job_posts.

Backfill pass that re-applies the live canonicalizers to every JobPost.
CC-139 extended it from a link-only pass to also rewrite apply_url —
this pins both legs plus the separate change counts in the summary.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from job_hunting.models import Company, JobPost, ScrapeProfile
from job_hunting.models.job_post_dedupe import _profile_url_rewrites_for_host


User = get_user_model()


class RecanonicalizeJobPostsCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", password="p")
        self.company = Company.objects.create(name="Acme")
        _profile_url_rewrites_for_host.cache_clear()
        # url_rewrites rule stripping ripplehire's per-session token, so the
        # apply_url pass has a host-specific rule to apply (imp_id is global
        # and needs no profile).
        ScrapeProfile.objects.update_or_create(
            hostname="ripplehire.com",
            defaults={"url_rewrites": [{
                "match": r"([?&])token=[^&]*",
                "rewrite": r"\1token=",
            }]},
        )
        self.addCleanup(_profile_url_rewrites_for_host.cache_clear)

    def _run(self):
        out = StringIO()
        call_command("recanonicalize_job_posts", stdout=out)
        return out.getvalue()

    def _seed_raw(self, **fields):
        """Insert a JobPost with a pre-polluted stored value, bypassing the
        canonicalize-at-write helpers via queryset.update() so the backfill
        has something to rewrite."""
        jp = JobPost.objects.create(
            title="Role", company=self.company, created_by=self.user,
            link="https://example.com/j/seed",
        )
        JobPost.objects.filter(pk=jp.pk).update(**fields)
        jp.refresh_from_db()
        return jp

    def test_apply_url_with_tracking_gets_rewritten(self):
        jp = self._seed_raw(
            apply_url="https://apply.ripplehire.com/j/1?token=STALE",
        )
        self._run()
        jp.refresh_from_db()
        self.assertEqual(
            jp.apply_url, "https://apply.ripplehire.com/j/1?token="
        )

    def test_apply_url_imp_id_stripped(self):
        jp = self._seed_raw(
            apply_url="https://greenhouse.io/acme/jobs/9?imp_id=abc",
        )
        self._run()
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url, "https://greenhouse.io/acme/jobs/9")

    def test_canonical_link_and_apply_url_both_rewritten(self):
        jp = self._seed_raw(
            link="https://jobright.ai/jobs/info/x?imp_id=one",
            canonical_link="https://jobright.ai/jobs/info/x?imp_id=one",
            apply_url="https://greenhouse.io/acme/jobs/9?imp_id=two",
        )
        self._run()
        jp.refresh_from_db()
        self.assertEqual(jp.canonical_link, "https://jobright.ai/jobs/info/x")
        self.assertEqual(jp.apply_url, "https://greenhouse.io/acme/jobs/9")

    def test_clean_apply_url_untouched(self):
        jp = self._seed_raw(
            apply_url="https://greenhouse.io/acme/jobs/clean",
        )
        self._run()
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url, "https://greenhouse.io/acme/jobs/clean")

    def test_null_apply_url_skipped(self):
        jp = self._seed_raw(apply_url=None)
        # Must not raise or write anything for a NULL apply_url.
        self._run()
        jp.refresh_from_db()
        self.assertIsNone(jp.apply_url)

    def test_summary_reports_apply_url_count_separately(self):
        self._seed_raw(apply_url="https://greenhouse.io/acme/jobs/9?imp_id=a")
        output = self._run()
        self.assertIn("apply_url updated", output)
        self.assertIn("canonical_link updated", output)
