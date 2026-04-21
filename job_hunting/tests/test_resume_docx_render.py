from io import BytesIO
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    Description,
    Education,
    Experience,
    ExperienceDescription,
    Project,
    Resume,
    ResumeEducation,
    ResumeExperience,
    ResumeProject,
)


User = get_user_model()


class TestResumeDocxRender(TestCase):
    """python-docx renderer produces a valid .docx for a populated resume
    without touching docxtpl. Smoke test: issue the endpoint, confirm the
    content-type and that the bytes parse back as a docx."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="dr",
            password="pw",
            first_name="Dr",
            last_name="Ender",
            email="dr@example.com",
        )
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.resume = Resume.objects.create(
            user=self.user, title="Senior Widget Engineer"
        )

        # Experience + 2 bullets
        exp = Experience.objects.create(
            title="Widget Lead",
            company=self.company,
            location="Austin, TX",
            summary="Led the widget team.",
        )
        ResumeExperience.objects.create(resume_id=self.resume.id, experience_id=exp.id)
        for text in ["Shipped widget v2", "Cut widget latency by 40%"]:
            d = Description.objects.create(content=text)
            ExperienceDescription.objects.create(
                experience_id=exp.id, description_id=d.id
            )

        edu = Education.objects.create(
            institution="MIT", degree="BS", major="CS"
        )
        ResumeEducation.objects.create(resume_id=self.resume.id, education_id=edu.id)

        proj = Project.objects.create(title="Widget CLI", user=self.user)
        ResumeProject.objects.create(resume_id=self.resume.id, project_id=proj.id)

    def test_export_returns_valid_docx(self):
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "officedocument.wordprocessingml.document",
            resp["Content-Type"],
        )
        # Parse the bytes back to confirm it's a real docx.
        from docx import Document

        doc = Document(BytesIO(resp.content))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Senior Widget Engineer", text)
        self.assertIn("Widget Lead", text)
        self.assertIn("Acme", text)
        self.assertIn("Shipped widget v2", text)
        self.assertIn("MIT", text)
        self.assertIn("Widget CLI", text)

    def test_markdown_route_is_authoritative(self):
        """Markdown export lives at the dedicated /markdown/ route, not
        on /export/?format=md (DRF reserves the `format` query param)."""
        resp = self.client.get(
            f"/api/v1/resumes/{self.resume.id}/markdown/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/markdown", resp["Content-Type"])
