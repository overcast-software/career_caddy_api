from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework import status

from job_hunting.models import Resume


class TestIngestEndpointBlob(APITestCase):
    """API-level tests for the async resume ingest endpoint."""

    def setUp(self):
        User = get_user_model()
        self.username = "testuser"
        self.password = "testpass123"
        self.user = User.objects.create_user(
            username=self.username, email="testuser@example.com", password=self.password
        )
        token = self._obtain_jwt(self.username, self.password)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _obtain_jwt(self, username, password):
        resp = self.client.post(
            "/api/v1/token/",
            data={"username": username, "password": password},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.data["access"]

    def test_ingest_returns_202_with_pending_resume(self):
        """Ingest creates a pending resume and returns 202 immediately."""
        docx_file = SimpleUploadedFile(
            "test_resume.docx",
            b"fake docx content",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        response = self.client.post(
            "/api/v1/resumes/ingest/",
            data={"file": docx_file},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("data", response.data)
        self.assertEqual(response.data["data"]["type"], "resume")
        self.assertEqual(response.data["data"]["attributes"]["status"], "pending")

        resume_id = int(response.data["data"]["id"])
        resume = Resume.objects.get(pk=resume_id)
        self.assertEqual(resume.user_id, self.user.id)
        self.assertEqual(resume.status, "pending")
        self.assertEqual(resume.name, "test_resume")

    def test_ingest_derives_name_from_filename(self):
        """Resume name is derived from the uploaded filename without .docx extension."""
        docx_file = SimpleUploadedFile(
            "My Professional Resume.docx",
            b"fake content",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        response = self.client.post(
            "/api/v1/resumes/ingest/",
            data={"file": docx_file},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        resume_id = int(response.data["data"]["id"])
        resume = Resume.objects.get(pk=resume_id)
        self.assertEqual(resume.name, "My Professional Resume")

    def test_ingest_missing_file(self):
        response = self.client.post(
            "/api/v1/resumes/ingest/", data={}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No file uploaded", str(response.data))

    def test_ingest_invalid_file_type(self):
        txt_file = SimpleUploadedFile(
            "test_resume.txt", b"plain text content", content_type="text/plain"
        )
        response = self.client.post(
            "/api/v1/resumes/ingest/",
            data={"file": txt_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only .docx files are supported", str(response.data))

    def test_ingest_requires_auth(self):
        self.client.credentials()
        docx_file = SimpleUploadedFile(
            "test_resume.docx",
            b"fake docx content",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response = self.client.post(
            "/api/v1/resumes/ingest/",
            data={"file": docx_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_ingest_no_included_in_response(self):
        """Async ingest returns only the pending resume, no included relationships."""
        docx_file = SimpleUploadedFile(
            "test_resume.docx",
            b"fake content",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response = self.client.post(
            "/api/v1/resumes/ingest/",
            data={"file": docx_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotIn("included", response.data)
