"""CC-78 — NanoID-PK foundation, proven on the Skill model.

These tests run against the post-migration schema (the test DB is built by
applying every migration, including 0114_skill_nanoid_pk_swap — so a broken
swap fails the suite at DB-build time). They assert the reusable
``NanoIDModel`` contract and that Skill's PK is now an opaque NanoID with
its dependent FK (``ResumeSkill.skill``) intact.
"""

import re
import string

from django.db import IntegrityError, transaction
from django.test import TestCase

from job_hunting.models import (
    NANOID_ALPHABET,
    NANOID_REGEX,
    NANOID_SIZE,
    NanoIDModel,
    Resume,
    ResumeSkill,
    Skill,
    generate_nanoid,
)


class GenerateNanoidTests(TestCase):
    def test_format(self):
        nid = generate_nanoid()
        self.assertEqual(len(nid), NANOID_SIZE)
        self.assertEqual(len(nid), 10)
        self.assertTrue(re.fullmatch(NANOID_REGEX, nid), nid)
        self.assertTrue(all(c in NANOID_ALPHABET for c in nid), nid)

    def test_alphabet_is_url_safe_alnum(self):
        # 62 url-safe alphanumerics, no "-"/"_" — the frontend/extension
        # validate ids against exactly this set.
        self.assertEqual(len(NANOID_ALPHABET), 62)
        self.assertEqual(
            NANOID_ALPHABET,
            string.digits + string.ascii_uppercase + string.ascii_lowercase,
        )
        self.assertNotIn("-", NANOID_ALPHABET)
        self.assertNotIn("_", NANOID_ALPHABET)

    def test_uniqueness_over_a_batch(self):
        ids = {generate_nanoid() for _ in range(5000)}
        # 62**10 keyspace — 5000 draws should never collide.
        self.assertEqual(len(ids), 5000)


class SkillNanoIdPkTests(TestCase):
    def test_skill_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Skill, NanoIDModel))
        pk_field = Skill._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_skill_gets_nanoid_pk(self):
        s = Skill.objects.create(text="Python", skill_type="lang")
        self.assertIsInstance(s.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, s.pk), s.pk)
        # Round-trips through the DB as the same opaque string id.
        self.assertEqual(Skill.objects.get(pk=s.pk).text, "Python")

    def test_distinct_pks(self):
        a = Skill.objects.create(text="A")
        b = Skill.objects.create(text="B")
        self.assertNotEqual(a.pk, b.pk)


class ResumeSkillForeignKeyTests(TestCase):
    def test_fk_to_nanoid_skill_round_trips(self):
        resume = Resume.objects.create(favorite=False)
        skill = Skill.objects.create(text="Django", skill_type="framework")
        rs = ResumeSkill.objects.create(resume=resume, skill=skill)

        # FK column carries the parent NanoID, and traversal both ways works.
        rs.refresh_from_db()
        self.assertEqual(rs.skill_id, skill.pk)
        self.assertEqual(rs.skill.text, "Django")
        self.assertEqual(list(skill.resume_skills.all()), [rs])

    def test_unique_together_still_enforced(self):
        resume = Resume.objects.create(favorite=False)
        skill = Skill.objects.create(text="SQL")
        ResumeSkill.objects.create(resume=resume, skill=skill)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ResumeSkill.objects.create(resume=resume, skill=skill)
