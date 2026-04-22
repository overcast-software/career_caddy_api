"""
Regression tests for bullet ordering + cross-resume isolation in the
ingest pipeline.

What this covers:
  - ResumeExperience.order is populated per experience.
  - ExperienceDescription.order is populated per bullet.
  - Two resumes with an identical bullet do NOT share a Description row
    via get_or_create — each resume owns its bullets independently.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from job_hunting.lib.services.ingest_resume import (
    CompanyOut,
    ExperienceOut,
    IngestResume,
    ParsedResume,
)
from job_hunting.models import (
    Description,
    ExperienceDescription,
    ResumeExperience,
)


def _build_parsed_resume(experiences):
    return ParsedResume(
        name="Test User",
        title="Engineer",
        phone=None,
        email=None,
        experiences=experiences,
    )


class _StubResult:
    def __init__(self, output):
        self.output = output

    def usage(self):
        return {}


class TestIngestOrdering(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create(username="test-ordering")

    def _run_ingest(self, parsed: ParsedResume, resume_name: str):
        stub_agent = MagicMock()
        stub_agent.run_sync.return_value = _StubResult(output=parsed)
        ingest = IngestResume(
            user=self.user, resume=b"fake", resume_name=resume_name, agent=stub_agent
        )
        with patch.object(
            IngestResume, "_extract_text", return_value="ignored-markdown"
        ):
            return ingest.process()

    def test_experience_and_bullet_order_persisted(self):
        parsed = _build_parsed_resume(
            [
                ExperienceOut(
                    title="Senior Engineer",
                    company=CompanyOut(name="Acme"),
                    summary=None,
                    bullets=["First bullet", "Second bullet", "Third bullet"],
                ),
                ExperienceOut(
                    title="Junior Engineer",
                    company=CompanyOut(name="Acme"),
                    summary=None,
                    bullets=["Older first", "Older second"],
                ),
            ]
        )
        resume = self._run_ingest(parsed, "r1.docx")

        joins = list(
            ResumeExperience.objects.filter(resume_id=resume.id).order_by("order")
        )
        self.assertEqual(len(joins), 2)
        self.assertEqual(joins[0].order, 0)
        self.assertEqual(joins[1].order, 1)
        self.assertEqual(joins[0].experience.title, "Senior Engineer")
        self.assertEqual(joins[1].experience.title, "Junior Engineer")

        senior_bullets = list(
            ExperienceDescription.objects.filter(
                experience_id=joins[0].experience_id
            ).order_by("order")
        )
        self.assertEqual([b.order for b in senior_bullets], [0, 1, 2])
        self.assertEqual(
            [b.description.content for b in senior_bullets],
            ["First bullet", "Second bullet", "Third bullet"],
        )

    def test_identical_bullet_text_does_not_leak_across_resumes(self):
        """
        Before the fix, Description.objects.get_or_create(content=...) reused
        the same Description row for identical bullet text, and
        ExperienceDescription.get_or_create silently no-op'd on the second
        resume — so a shared bullet ended up attached to the wrong resume's
        experience when queried in some orderings. After the fix, each
        ingest creates fresh Description and join rows.
        """
        shared_bullet = "Led a cross-functional team of five"
        parsed_a = _build_parsed_resume(
            [
                ExperienceOut(
                    title="Eng A",
                    company=CompanyOut(name="Acme"),
                    summary=None,
                    bullets=[shared_bullet],
                )
            ]
        )
        parsed_b = _build_parsed_resume(
            [
                ExperienceOut(
                    title="Eng B",
                    company=CompanyOut(name="Beta"),
                    summary=None,
                    bullets=[shared_bullet],
                )
            ]
        )
        resume_a = self._run_ingest(parsed_a, "a.docx")
        resume_b = self._run_ingest(parsed_b, "b.docx")

        desc_rows = Description.objects.filter(content=shared_bullet)
        self.assertEqual(desc_rows.count(), 2, "each resume owns its bullets")

        # Resume A's experience is linked only to one of the two Description
        # rows, and Resume B's to the other.
        exp_a_id = (
            ResumeExperience.objects.get(resume_id=resume_a.id).experience_id
        )
        exp_b_id = (
            ResumeExperience.objects.get(resume_id=resume_b.id).experience_id
        )
        a_descs = set(
            ExperienceDescription.objects.filter(
                experience_id=exp_a_id
            ).values_list("description_id", flat=True)
        )
        b_descs = set(
            ExperienceDescription.objects.filter(
                experience_id=exp_b_id
            ).values_list("description_id", flat=True)
        )
        self.assertEqual(len(a_descs), 1)
        self.assertEqual(len(b_descs), 1)
        self.assertTrue(a_descs.isdisjoint(b_descs), "no shared Description row")

    def test_empty_bullets_list_ok(self):
        parsed = _build_parsed_resume(
            [
                ExperienceOut(
                    title="No bullets",
                    company=CompanyOut(name="Acme"),
                    summary=None,
                    bullets=[],
                )
            ]
        )
        resume = self._run_ingest(parsed, "r.docx")
        self.assertEqual(
            ResumeExperience.objects.filter(resume_id=resume.id).count(), 1
        )
        exp_id = ResumeExperience.objects.get(resume_id=resume.id).experience_id
        self.assertEqual(
            ExperienceDescription.objects.filter(experience_id=exp_id).count(), 0
        )
