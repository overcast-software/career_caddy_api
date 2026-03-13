from django.test import TestCase
from job_hunting.models import Description


class DescriptionModelTests(TestCase):
    def test_create_description(self):
        d = Description.objects.create(content="Led backend team")
        self.assertEqual(d.content, "Led backend team")
        self.assertEqual(Description.objects.get(pk=d.pk).content, "Led backend team")

    def test_content_nullable(self):
        d = Description.objects.create(content=None)
        self.assertIsNone(Description.objects.get(pk=d.pk).content)
