"""CC-204 — resume ingest persists the upload to durable storage + enqueues.

The ingest endpoint now saves the uploaded file to ``Resume.file`` via
``default_storage`` (Wasabi S3 in prod / local FileSystemStorage on self-host)
and enqueues only ``resume_id`` — the bytes NEVER ride the async payload. The
``resume_parse_job`` worker reads the blob back from storage.

Tests use a temp-dir FileSystemStorage override so they exercise the real
default_storage round-trip WITHOUT hitting Wasabi. ``file_path`` (the original
filename marker) is retained for back-compat.
"""
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.models import Resume

User = get_user_model()

_TMP_MEDIA = tempfile.mkdtemp(prefix="cc204-media-")

# Force the local filesystem backend (temp dir) for the whole class so the
# real default_storage.save round-trip runs without touching Wasabi/S3.
_LOCAL_STORAGES = override_settings(
    MEDIA_ROOT=_TMP_MEDIA,
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    },
)


@_LOCAL_STORAGES
class TestResumeIngestStoresBlobAndEnqueues(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="ingester", password="pw")
        self.client.force_authenticate(user=self.user)

    def _ingest(self, name="Jane_Doe_Resume.docx", content=b"fake-docx-bytes"):
        ctype = (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        )
        uploaded = SimpleUploadedFile(name, content, content_type=ctype)
        # Patch the enqueue seam — the real worker runs out-of-band.
        with patch(
            "job_hunting.api.views.resumes.enqueue"
        ) as mock_enqueue:
            resp = self.client.post(
                "/api/v1/resumes/ingest/",
                data={"file": uploaded},
                format="multipart",
            )
        return resp, mock_enqueue

    def test_upload_persists_blob_to_storage(self):
        resp, _ = self._ingest(content=b"the-real-docx-bytes")
        self.assertEqual(resp.status_code, 202, resp.json())
        resume = Resume.objects.filter(user_id=self.user.id).first()
        self.assertIsNotNone(resume)
        # The blob is durably stored and reads back byte-identical.
        self.assertTrue(resume.file)
        with resume.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"the-real-docx-bytes")

    def test_file_path_still_records_original_filename(self):
        resp, _ = self._ingest(name="Jane_Doe_Resume.docx")
        self.assertEqual(resp.status_code, 202)
        resume = Resume.objects.filter(user_id=self.user.id).first()
        self.assertEqual(resume.file_path, "Jane_Doe_Resume.docx")

    def test_enqueues_resume_ingest_with_resume_id_only_no_bytes(self):
        resp, mock_enqueue = self._ingest()
        self.assertEqual(resp.status_code, 202)
        resume = Resume.objects.filter(user_id=self.user.id).first()
        mock_enqueue.assert_called_once()
        args, kwargs = mock_enqueue.call_args
        self.assertEqual(args[0], "resume_ingest")
        self.assertEqual(kwargs["resume_id"], resume.id)
        # The bytes must NOT ride the payload — that was the whole blocker.
        self.assertNotIn("file_blob", kwargs)
        payload_values = list(kwargs.values())
        self.assertFalse(
            any(isinstance(v, (bytes, bytearray)) for v in payload_values),
            msg=f"no bytes may ride the enqueue payload; got {kwargs!r}",
        )

    def test_rejects_unsupported_extension(self):
        resp, mock_enqueue = self._ingest(name="notes.txt")
        self.assertEqual(resp.status_code, 400)
        mock_enqueue.assert_not_called()


@_LOCAL_STORAGES
class TestResumeParseJobReadsFromStorage(TestCase):
    """The worker reads the blob back from Resume.file (not from the payload)."""

    def setUp(self):
        self.user = User.objects.create_user(username="worker", password="pw")

    def test_worker_reads_blob_from_storage_and_ingests(self):
        from job_hunting.lib import tasks

        resume = Resume.objects.create(
            user_id=self.user.id,
            name="Jane",
            file_path="Jane_Doe_Resume.docx",
            status="pending",
        )
        resume.file.save(
            "Jane_Doe_Resume.docx",
            SimpleUploadedFile("Jane_Doe_Resume.docx", b"docx-blob-xyz"),
            save=True,
        )

        seen = {}

        class _FakeIngest:
            def __init__(self, *, user, resume, resume_name, agent, db_resume):
                seen["blob"] = resume
                seen["resume_name"] = resume_name

            def process(self):
                seen["processed"] = True

        with patch(
            "job_hunting.lib.services.ingest_resume.IngestResume", _FakeIngest
        ):
            result = tasks.resume_parse_job(resume.id, derived_name="Jane")

        self.assertEqual(result["status"], "completed")
        # The worker read the EXACT stored bytes and passed the filename.
        self.assertEqual(seen["blob"], b"docx-blob-xyz")
        self.assertEqual(seen["resume_name"], "Jane_Doe_Resume.docx")
        self.assertTrue(seen["processed"])
        resume.refresh_from_db()
        self.assertEqual(resume.status, "completed")

    def test_worker_fails_cleanly_when_no_stored_file(self):
        from job_hunting.lib import tasks

        resume = Resume.objects.create(
            user_id=self.user.id, name="NoFile", status="pending"
        )
        result = tasks.resume_parse_job(resume.id)
        self.assertEqual(result["status"], "failed")
        resume.refresh_from_db()
        self.assertEqual(resume.status, "failed")

    def test_worker_missing_row_is_terminal(self):
        from job_hunting.lib import tasks

        result = tasks.resume_parse_job("nonexistent")
        self.assertEqual(result["status"], "missing")


class TestStorageEnvGate(TestCase):
    """The S3/local backend selection is env-gated (keyless self-host default)."""

    def test_absent_bucket_env_uses_filesystem_storage(self):
        # With no AWS_STORAGE_BUCKET_NAME the default storage is local FS — the
        # keyless self-host path. settings.STORAGES resolves this at import; in
        # the test env no bucket is set, so the default backend is FileSystem.
        from django.conf import settings

        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )
