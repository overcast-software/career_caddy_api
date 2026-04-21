from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Resume


User = get_user_model()


class TestResumeIngestRecordsOriginalFilename(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ingester", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_upload_stores_original_filename_on_file_path(self):
        """The resume import endpoint should stash the uploaded filename on
        Resume.file_path so the user can later see where a resume came from.
        The server does not persist the blob — file_path is a reference,
        not a link target."""
        # Patch the background ingest worker — we only care about the
        # placeholder Resume row that gets created synchronously.
        with patch("job_hunting.api.views.resumes.IngestResume") as _MockIngest:
            _MockIngest.return_value.process.return_value = None
            uploaded = SimpleUploadedFile(
                "Jane_Doe_Resume.docx",
                b"fake-docx-bytes",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            response = self.client.post(
                "/api/v1/resumes/ingest/",
                data={"file": uploaded},
                format="multipart",
            )

        self.assertEqual(response.status_code, 202, response.json())
        resume = Resume.objects.filter(user_id=self.user.id).first()
        self.assertIsNotNone(resume)
        self.assertEqual(resume.file_path, "Jane_Doe_Resume.docx")
