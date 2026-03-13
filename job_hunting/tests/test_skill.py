from django.test import TestCase
from job_hunting.models import Skill


class SkillModelTests(TestCase):
    def test_create_skill(self):
        s = Skill.objects.create(text="Python", skill_type="Language")
        self.assertEqual(s.text, "Python")
        self.assertEqual(s.skill_type, "Language")

    def test_to_export_value(self):
        s = Skill.objects.create(text="Python", skill_type="Language")
        self.assertEqual(s.to_export_value(), {"text": "Python", "skill_type": "Language"})

    def test_skill_type_nullable(self):
        s = Skill.objects.create(text="pytest")
        self.assertIsNone(s.skill_type)
        fetched = Skill.objects.get(pk=s.pk)
        self.assertIsNone(fetched.skill_type)
