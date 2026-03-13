from datetime import date
from django.test import TestCase
from job_hunting.models import Education


class EducationModelTests(TestCase):
    def test_create_education(self):
        e = Education.objects.create(degree="B.S.", institution="MIT", major="CS", issue_date=date(2015, 5, 1))
        self.assertEqual(e.degree, "B.S.")
        self.assertEqual(e.institution, "MIT")

    def test_to_export_dict(self):
        e = Education.objects.create(degree="B.S.", institution="MIT", major="CS", issue_date=date(2015, 5, 1))
        d = e.to_export_dict()
        self.assertEqual(d["degree"], "B.S.")
        self.assertEqual(d["institution"], "MIT")
        self.assertEqual(d["issue_date"], "2015-05-01")

    def test_optional_minor(self):
        e = Education.objects.create(institution="MIT")
        self.assertIsNone(e.minor)
