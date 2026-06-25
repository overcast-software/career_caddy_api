"""CC-77 #79 — Resume integer PK -> 10-char NanoID PK (true PK swap).

These tests run against the post-migration schema: the test DB is built by
applying every migration, including ``0123_resume_nanoid_pk_swap``. A broken
forward swap fails the whole suite at DB-build time, so importing + querying
Resume is itself a smoke test of the migration.

Beyond the NanoIDModel contract we assert that all nine FKs that reference
``resume(id)`` round-trip with the NanoID value and traverse both ways, that
CASCADE / SET_NULL on-delete behave, and that the two composite UNIQUEs that
ride on ``resume_id`` (``resume_skill`` unique_together and the
``unique_score_per_job_resume_user`` score constraint) survive on the NanoID
columns.
"""

import re

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    Certification,
    CoverLetter,
    Education,
    Experience,
    JobApplication,
    JobPost,
    NanoIDModel,
    Project,
    Resume,
    ResumeCertification,
    ResumeEducation,
    ResumeExperience,
    ResumeProject,
    ResumeSkill,
    ResumeSummary,
    Score,
    Skill,
    Summary,
)

User = get_user_model()


class ResumeNanoIdPkContractTests(TestCase):
    def test_resume_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Resume, NanoIDModel))
        pk_field = Resume._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_resume_gets_nanoid_pk(self):
        r = Resume.objects.create(title="Backend SWE")
        self.assertIsInstance(r.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, r.pk), r.pk)
        self.assertEqual(Resume.objects.get(pk=r.pk).title, "Backend SWE")

    def test_distinct_pks(self):
        a = Resume.objects.create()
        b = Resume.objects.create()
        self.assertNotEqual(a.pk, b.pk)


class ResumeCascadeJoinForeignKeyTests(TestCase):
    """The six CASCADE join tables: FK round-trips + cascade delete."""

    def setUp(self):
        self.resume = Resume.objects.create(title="R")

    def test_resume_skill_fk_round_trips(self):
        skill = Skill.objects.create(text="Python")
        rs = ResumeSkill.objects.create(resume=self.resume, skill=skill)
        rs.refresh_from_db()
        self.assertEqual(rs.resume_id, self.resume.pk)
        self.assertIsInstance(rs.resume_id, str)
        self.assertEqual(list(self.resume.resume_skills.all()), [rs])

    def test_resume_summary_fk_round_trips(self):
        summary = Summary.objects.create(content="Sum")
        link = ResumeSummary.objects.create(resume=self.resume, summary=summary)
        link.refresh_from_db()
        self.assertEqual(link.resume_id, self.resume.pk)
        self.assertIsInstance(link.resume_id, str)

    def test_resume_certification_fk_round_trips(self):
        cert = Certification.objects.create(title="AWS")
        link = ResumeCertification.objects.create(resume=self.resume, certification=cert)
        link.refresh_from_db()
        self.assertEqual(link.resume_id, self.resume.pk)

    def test_resume_education_fk_round_trips(self):
        edu = Education.objects.create(institution="MIT")
        link = ResumeEducation.objects.create(resume=self.resume, education=edu)
        link.refresh_from_db()
        self.assertEqual(link.resume_id, self.resume.pk)

    def test_resume_experience_fk_round_trips(self):
        exp = Experience.objects.create(title="Engineer")
        link = ResumeExperience.objects.create(resume=self.resume, experience=exp)
        link.refresh_from_db()
        self.assertEqual(link.resume_id, self.resume.pk)

    def test_resume_project_fk_round_trips(self):
        proj = Project.objects.create(title="Caddy")
        link = ResumeProject.objects.create(resume=self.resume, project=proj)
        link.refresh_from_db()
        self.assertEqual(link.resume_id, self.resume.pk)

    def test_cascade_delete_removes_join_rows(self):
        skill = Skill.objects.create(text="Go")
        ResumeSkill.objects.create(resume=self.resume, skill=skill)
        ResumeEducation.objects.create(
            resume=self.resume, education=Education.objects.create()
        )
        self.assertEqual(ResumeSkill.objects.count(), 1)
        self.assertEqual(ResumeEducation.objects.count(), 1)
        self.resume.delete()
        self.assertEqual(ResumeSkill.objects.count(), 0)
        self.assertEqual(ResumeEducation.objects.count(), 0)


class ResumeSetNullForeignKeyTests(TestCase):
    """score / cover_letter / job_application: nullable FK + SET_NULL delete."""

    def setUp(self):
        self.resume = Resume.objects.create(title="R")

    def test_score_resume_fk_round_trips_and_set_null(self):
        s = Score.objects.create(resume=self.resume, score=5)
        s.refresh_from_db()
        self.assertEqual(s.resume_id, self.resume.pk)
        self.assertIsInstance(s.resume_id, str)
        self.assertEqual(list(self.resume.scores.all()), [s])
        self.resume.delete()
        s.refresh_from_db()
        self.assertIsNone(s.resume_id)

    def test_cover_letter_resume_fk_round_trips_and_set_null(self):
        cl = CoverLetter.objects.create(resume=self.resume, content="hi")
        cl.refresh_from_db()
        self.assertEqual(cl.resume_id, self.resume.pk)
        self.resume.delete()
        cl.refresh_from_db()
        self.assertIsNone(cl.resume_id)

    def test_job_application_resume_fk_round_trips_and_set_null(self):
        app = JobApplication.objects.create(resume=self.resume, status="Applied")
        app.refresh_from_db()
        self.assertEqual(app.resume_id, self.resume.pk)
        self.resume.delete()
        app.refresh_from_db()
        self.assertIsNone(app.resume_id)


class ResumeCompositeUniqueTests(TestCase):
    """Composite UNIQUEs that ride on resume_id survive the swap."""

    def test_resume_skill_unique_together(self):
        resume = Resume.objects.create()
        skill = Skill.objects.create(text="SQL")
        ResumeSkill.objects.create(resume=resume, skill=skill)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ResumeSkill.objects.create(resume=resume, skill=skill)

    def test_unique_score_per_job_resume_user(self):
        user = User.objects.create(username="cc79resume", email="r@example.com")
        jp = JobPost.objects.create(title="SRE")
        resume = Resume.objects.create()
        Score.objects.create(job_post=jp, resume=resume, user=user, score=5)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Score.objects.create(job_post=jp, resume=resume, user=user, score=6)
