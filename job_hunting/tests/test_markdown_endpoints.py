from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import CoverLetter, Resume

User = get_user_model()


class ResumeMarkdownEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="mduser", password="pass", email="md@example.com"
        )
        self.other = User.objects.create_user(username="other", password="pass")
        self.staff = User.objects.create_user(
            username="staff", password="pass", is_staff=True
        )
        self.resume = Resume.objects.create(
            user=self.user, title="SRE Resume", name="Jane Doe"
        )

    def test_returns_text_markdown_content_type(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/markdown/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/markdown"))

    def test_body_is_raw_markdown_not_json(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/markdown/")
        body = resp.content.decode()
        self.assertNotIn('"data":', body)
        self.assertIn("SRE Resume", body)

    def test_owner_can_read(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/markdown/")
        self.assertEqual(resp.status_code, 200)

    def test_other_user_forbidden(self):
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/markdown/")
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_read_other_users_resume(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/markdown/")
        self.assertEqual(resp.status_code, 200)

    def test_missing_resume_returns_404(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/resumes/999999/markdown/")
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_rejected(self):
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/markdown/")
        self.assertIn(resp.status_code, (401, 403))


class CoverLetterMarkdownEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="clowner", password="pass")
        self.other = User.objects.create_user(username="stranger", password="pass")
        self.staff = User.objects.create_user(
            username="staffer", password="pass", is_staff=True
        )
        self.letter = CoverLetter.objects.create(
            user=self.user,
            content="Dear hiring manager,\n\nI am interested...",
        )

    def test_returns_text_markdown(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(f"/api/v1/cover-letters/{self.letter.id}/markdown/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/markdown"))
        body = resp.content.decode()
        self.assertIn("Dear hiring manager", body)
        self.assertIn("# Cover Letter", body)

    def test_other_user_forbidden(self):
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(f"/api/v1/cover-letters/{self.letter.id}/markdown/")
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_read(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f"/api/v1/cover-letters/{self.letter.id}/markdown/")
        self.assertEqual(resp.status_code, 200)

    def test_missing_cover_letter_returns_404(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/cover-letters/999999/markdown/")
        self.assertEqual(resp.status_code, 404)

    def test_empty_content_renders_header_only(self):
        empty = CoverLetter.objects.create(user=self.user, content="")
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(f"/api/v1/cover-letters/{empty.id}/markdown/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("# Cover Letter", body)
