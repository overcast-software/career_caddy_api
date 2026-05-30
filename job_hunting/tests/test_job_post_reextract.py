from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.lib.parsers.job_post_extractor import ParsedJobData
from job_hunting.models import Company, JobPost, Scrape


def _inline_async_task(name, *args, **kwargs):
    """Stand-in for django_q.tasks.async_task that runs the target inline.

    Phase 5a of Plans/Job-queue integration replaced parse_scrape's
    threading.Thread spawn with an async_task enqueue. The reextract
    flow ends with parse_scrape(..., sync=False) which enqueues the
    parse_scrape_job task; under test we resolve that target inline
    so the assertions can read the post-parse JobPost row immediately.
    """
    import importlib

    module_path, _, func_name = name.rpartition(".")
    mod = importlib.import_module(module_path)
    return getattr(mod, func_name)(*args, **kwargs)


def _inline_reextract(fn):
    """Decorator: patches async_task in the parse_scrape module so the
    parse_scrape_job task runs inline within the request thread instead
    of being enqueued on the qcluster worker."""
    return patch(
        "job_hunting.lib.parsers.job_post_extractor.async_task",
        new=_inline_async_task,
    )(fn)


User = get_user_model()


def _make_extracted(**overrides):
    defaults = {
        "title": "Senior Engineer",
        "company_name": "ShouldBeIgnored",  # company is not reassigned
        "description": "Build great things.",
        "posted_date": datetime(2026, 4, 18),
        "salary_min": 175000,
        "salary_max": 215000,
        "location": "Remote",
        "remote": True,
    }
    defaults.update(overrides)
    return ParsedJobData(**defaults)


class TestJobPostReextractAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="reuser", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="OriginalCo")
        self.job_post = JobPost.objects.create(
            title="Old Title", company=self.company, created_by=self.user
        )
        self.url = f"/api/v1/job-posts/{self.job_post.id}/reextract/"

    @_inline_reextract
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_reextract_updates_fields(self, mock_extract):
        mock_extract.return_value = _make_extracted()
        response = self.client.post(self.url, data={"text": "pasted content"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.job_post.refresh_from_db()
        self.assertEqual(self.job_post.title, "Senior Engineer")
        self.assertEqual(self.job_post.description, "Build great things.")
        self.assertEqual(self.job_post.location, "Remote")
        self.assertTrue(self.job_post.remote)
        self.assertEqual(self.job_post.salary_min, Decimal("175000.00"))
        self.assertEqual(self.job_post.salary_max, Decimal("215000.00"))

    @_inline_reextract
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_reextract_does_not_reassign_company(self, mock_extract):
        mock_extract.return_value = _make_extracted()
        self.client.post(self.url, data={"text": "pasted content"}, format="json")
        self.job_post.refresh_from_db()
        self.assertEqual(self.job_post.company_id, self.company.id)

    @_inline_reextract
    @patch(
        "job_hunting.lib.parsers.completeness_reviewer.maybe_review_and_persist",
        return_value=None,
    )
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_reextract_persists_scrape_for_provenance(
        self, mock_extract, _mock_reviewer
    ):
        # The thin mock description ("Build great things.") would
        # otherwise trip CompletenessReviewer's "not a real job
        # description" rejection and flip scrape.status to failed —
        # this test is about provenance, not review behavior, so the
        # reviewer is stubbed out. (Before 2026-05-30 the old
        # _InlineThread patch crashed the reviewer's worker thread by
        # accident and the rejection silently never landed; the new
        # surgical async_task patch correctly runs the reviewer.)
        mock_extract.return_value = _make_extracted()
        self.client.post(self.url, data={"text": "pasted content"}, format="json")
        scrape = Scrape.objects.filter(job_post=self.job_post).first()
        self.assertIsNotNone(scrape)
        self.assertEqual(scrape.job_content, "pasted content")
        self.assertEqual(scrape.created_by, self.user)
        self.assertEqual(scrape.status, "completed")

    @_inline_reextract
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_reextract_marks_scrape_failed_on_extractor_error(self, mock_extract):
        mock_extract.side_effect = RuntimeError("LLM down")
        response = self.client.post(self.url, data={"text": "pasted content"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.filter(job_post=self.job_post).first()
        self.assertIsNotNone(scrape)
        self.assertEqual(scrape.status, "failed")

    def test_reextract_requires_text(self):
        response = self.client.post(self.url, data={"text": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reextract_404_for_unknown_job_post(self):
        response = self.client.post(
            "/api/v1/job-posts/9999/reextract/",
            data={"text": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_reextract_403_for_non_owner(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.post(self.url, data={"text": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @_inline_reextract
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_reextract_skips_none_fields(self, mock_extract):
        mock_extract.return_value = _make_extracted(
            description=None, location=None, remote=None,
        )
        self.job_post.description = "kept"
        self.job_post.location = "kept-loc"
        self.job_post.remote = False
        self.job_post.save()
        self.client.post(self.url, data={"text": "x"}, format="json")
        self.job_post.refresh_from_db()
        self.assertEqual(self.job_post.description, "kept")
        self.assertEqual(self.job_post.location, "kept-loc")
        self.assertFalse(self.job_post.remote)
