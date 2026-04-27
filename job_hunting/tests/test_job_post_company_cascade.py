"""PATCH /api/v1/job-posts/:id/ that moves the post to a different
Company should cascade the FK to all child tables that carry their
own company_id (Question, Scrape, CoverLetter, JobApplication).

Multi-tenant rationale: the dominant cause of a company change is
typo-correction on the original company creation. Leaving children
pinned to the wrong company makes the typo correction worse, not
better. Children's textual content is intentionally NOT rewritten —
it's write-once-and-forget in practice and rewrite is out of scope.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    CoverLetter,
    JobApplication,
    JobPost,
    Question,
    Scrape,
)


User = get_user_model()


class JobPostCompanyCascadeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company_a = Company.objects.create(name="Acme")
        self.company_b = Company.objects.create(name="Initech")
        self.jp = JobPost.objects.create(
            title="Engineer",
            company=self.company_a,
            link="https://acme.example/jobs/1",
            created_by=self.user,
        )
        self.question = Question.objects.create(
            content="Why us?",
            job_post=self.jp,
            company=self.company_a,
            created_by=self.user,
        )
        self.scrape = Scrape.objects.create(
            url="https://acme.example/jobs/1",
            job_post=self.jp,
            company=self.company_a,
            created_by=self.user,
        )
        self.cover_letter = CoverLetter.objects.create(
            content="Dear Acme,",
            job_post=self.jp,
            company=self.company_a,
            user=self.user,
        )
        self.application = JobApplication.objects.create(
            job_post=self.jp,
            company=self.company_a,
            user=self.user,
        )

    def _patch_company(self, company):
        body = {
            "data": {
                "type": "job-post",
                "id": str(self.jp.id),
                "relationships": {
                    "company": {
                        "data": {"type": "company", "id": str(company.id)}
                    }
                },
            }
        }
        return self.client.patch(
            f"/api/v1/job-posts/{self.jp.id}/", body, format="json"
        )

    def test_changing_company_cascades_to_all_four_children(self):
        resp = self._patch_company(self.company_b)
        self.assertEqual(resp.status_code, 200)
        self.jp.refresh_from_db()
        self.assertEqual(self.jp.company_id, self.company_b.id)
        for child in (self.question, self.scrape, self.cover_letter, self.application):
            child.refresh_from_db()
            self.assertEqual(
                child.company_id,
                self.company_b.id,
                f"{type(child).__name__} did not cascade",
            )

    def test_no_cascade_when_company_unchanged(self):
        """Patching another field shouldn't touch child company_ids — guards
        against an accidental UPDATE storm on every post edit."""
        body = {
            "data": {
                "type": "job-post",
                "id": str(self.jp.id),
                "attributes": {"title": "Senior Engineer"},
            }
        }
        resp = self.client.patch(
            f"/api/v1/job-posts/{self.jp.id}/", body, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        for child in (self.question, self.scrape, self.cover_letter, self.application):
            child.refresh_from_db()
            self.assertEqual(child.company_id, self.company_a.id)
