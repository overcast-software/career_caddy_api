from django.test import TestCase
from job_hunting.models import Status


class StatusModelTests(TestCase):
    def test_create_status(self):
        s = Status.objects.create(status="Applied", status_type="application")
        self.assertEqual(s.status, "Applied")
        self.assertEqual(s.status_type, "application")
        fetched = Status.objects.get(pk=s.pk)
        self.assertEqual(fetched.status, "Applied")
        self.assertEqual(fetched.status_type, "application")

    def test_status_type_nullable(self):
        s = Status.objects.create(status="Rejected")
        self.assertIsNone(s.status_type)
        fetched = Status.objects.get(pk=s.pk)
        self.assertIsNone(fetched.status_type)

    def test_str_repr(self):
        s = Status(status="Interviewing")
        self.assertIn("Interviewing", str(s))

    def test_created_at_auto(self):
        s = Status.objects.create(status="Offer")
        self.assertIsNotNone(s.created_at)
