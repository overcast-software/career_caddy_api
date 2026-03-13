from datetime import date
from django.test import TestCase
from job_hunting.models import Certification


class CertificationModelTests(TestCase):
    def test_create_certification(self):
        c = Certification.objects.create(issuer="AWS", title="SAA", issue_date=date(2023, 1, 1), content="Cloud cert")
        self.assertEqual(c.issuer, "AWS")
        self.assertEqual(c.title, "SAA")
        self.assertEqual(c.issue_date, date(2023, 1, 1))

    def test_to_export_dict(self):
        c = Certification.objects.create(issuer="AWS", title="SAA", issue_date=date(2023, 1, 1), content="x")
        d = c.to_export_dict()
        self.assertEqual(d["issuer"], "AWS")
        self.assertEqual(d["title"], "SAA")
        self.assertEqual(d["issue_date"], "2023-01-01")
        self.assertEqual(d["content"], "x")

    def test_nullable_fields(self):
        c = Certification.objects.create()
        self.assertIsNone(c.issuer)
        self.assertIsNone(c.issue_date)
