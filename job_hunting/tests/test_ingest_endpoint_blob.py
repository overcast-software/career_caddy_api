import tempfile
import unittest
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework import status

from job_hunting.lib.db import init_sqlalchemy
from job_hunting.lib.models.base import BaseModel, Base
from job_hunting.lib.models import Resume


class TestIngestEndpointBlob(APITestCase):
    """API-level tests for the resume ingest endpoint using blob uploads."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ensure SQLAlchemy is initialized and tables exist in the Django test DB
        init_sqlalchemy()
        cls.session = BaseModel.get_session()
        cls.engine = cls.session.bind
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        # Clean up tables after all tests in this class
        Base.metadata.drop_all(bind=cls.engine)
        super().tearDownClass()

    def setUp(self):
        # Hard reset SA tables for isolation between tests
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

        # Create a Django user and authenticate with JWT for protected endpoints
        User = get_user_model()
        self.username = "testuser"
        self.password = "testpass123"
        self.user = User.objects.create_user(
            username=self.username, email="testuser@example.com", password=self.password
        )
        token = self._obtain_jwt(self.username, self.password)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        # Create a test resume
        self.resume = Resume(user_id=self.user.id, file_path="/tmp/test.docx")
        self.resume.save()

    def _obtain_jwt(self, username, password):
        resp = self.client.post(
            "/api/v1/token/",
            data={"username": username, "password": password},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.data["access"]

    @patch("job_hunting.api.views.IngestResume")
    def test_blob_ingest_creates_resume_and_response(self, mock_ingest_class):
        """Test successful blob upload to new ingest endpoint creates resume."""
        # Setup mock IngestResume
        mock_ingest_instance = MagicMock()
        stub_resume = Resume()
        stub_resume.user_id = self.user.id
        stub_resume.save()
        mock_ingest_instance.process.return_value = stub_resume
        mock_ingest_class.return_value = mock_ingest_instance

        # Create a fake .docx file upload
        docx_content = b"fake docx binary content for testing"
        uploaded_file = SimpleUploadedFile(
            "test_resume.docx",
            docx_content,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        # POST to new ingest endpoint (no resume id)
        response = self.client.post(
            "/api/v1/resumes/ingest/",
            data={"file": uploaded_file},
            format="multipart",
        )

        # Verify response
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("data", response.data)
        self.assertEqual(response.data["data"]["type"], "resume")

        # Verify a new resume was created with correct user_id
        created_resume_id = int(response.data["data"]["id"])
        created_resume = Resume.get(created_resume_id)
        self.assertIsNotNone(created_resume)
        self.assertEqual(created_resume.user_id, self.user.id)

        # Verify IngestResume was called with blob content and user
        mock_ingest_class.assert_called_once()
        call_args = mock_ingest_class.call_args
        self.assertEqual(call_args[1]["resume"], docx_content)
        self.assertEqual(call_args[1]["user"], self.user)
        self.assertIsNone(call_args[1]["agent"])

        # Verify process was called
        mock_ingest_instance.process.assert_called_once()

    def test_blob_ingest_missing_file(self):
        """Test error when no file is uploaded."""
        response = self.client.post(
            "/api/v1/resumes/ingest/", data={}, format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No file uploaded", str(response.data))

    def test_blob_ingest_invalid_file_type(self):
        """Test error when non-.docx file is uploaded."""
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

    def test_blob_ingest_requires_auth(self):
        """Test that ingest endpoint requires authentication."""
        # Remove authentication
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

    @patch("job_hunting.api.views.IngestResume")
    def test_blob_ingest_processing_error(self, mock_ingest_class):
        """Test error handling when IngestResume.process() fails."""
        # Setup mock to raise exception during processing
        mock_ingest_instance = MagicMock()
        mock_ingest_instance.process.side_effect = Exception("Processing failed")
        mock_ingest_class.return_value = mock_ingest_instance

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

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Failed to process resume", str(response.data))
        self.assertIn("Processing failed", str(response.data))

    @patch("job_hunting.api.views.BaseSAViewSet._build_included")
    @patch("job_hunting.api.views.IngestResume")
    def test_blob_ingest_with_includes(self, mock_ingest_class, mock_build_included):
        """Test ingest endpoint with ?include parameter."""
        # Prepare a stub included payload
        included_stub = [{"type": "summary", "id": "123"}]
        mock_build_included.return_value = included_stub

        # Setup mock IngestResume
        mock_ingest_instance = MagicMock()
        stub_resume = Resume()
        stub_resume.user_id = self.user.id
        stub_resume.save()
        mock_ingest_instance.process.return_value = stub_resume
        mock_ingest_class.return_value = mock_ingest_instance

        docx_file = SimpleUploadedFile(
            "test_resume.docx",
            b"fake docx content",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        # Request with includes
        response = self.client.post(
            "/api/v1/resumes/ingest/?include=experiences,summaries",
            data={"file": docx_file},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("data", response.data)
        self.assertEqual(response.data["data"]["type"], "resume")
        self.assertIn("included", response.data)
        self.assertEqual(response.data["included"], included_stub)
        mock_build_included.assert_called_once()
        
        # Verify include_rels was truthy
        include_rels = mock_build_included.call_args[0][2]
        self.assertTrue(include_rels)
