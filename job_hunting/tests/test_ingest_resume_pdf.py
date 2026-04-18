import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from job_hunting.lib.services.ingest_resume import IngestResume


def _install_fake_pypdf(pages_text):
    """Stub pypdf.PdfReader so tests don't require the real package at import time."""
    pages = []
    for text in pages_text:
        p = MagicMock()
        p.extract_text.return_value = text
        pages.append(p)
    reader_instance = MagicMock()
    reader_instance.pages = pages
    reader_class = MagicMock(return_value=reader_instance)

    mod = types.ModuleType("pypdf")
    mod.PdfReader = reader_class
    return mod, reader_class


class TestIngestResumePdf(unittest.TestCase):
    PAGES = ["# PDF Resume", "Second page body."]

    def setUp(self):
        mod, reader_class = _install_fake_pypdf(self.PAGES)
        self._saved = sys.modules.get("pypdf")
        sys.modules["pypdf"] = mod
        self.reader_class = reader_class

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = self._saved

    def test_extract_text_from_pdf_blob(self):
        result = IngestResume().extract_text_from_pdf(b"%PDF-1.4 fake")
        self.assertEqual(result, "# PDF Resume\n\nSecond page body.")
        # Reader was constructed with a BytesIO wrapper, not the raw bytes
        call_arg = self.reader_class.call_args[0][0]
        self.assertTrue(hasattr(call_arg, "read"))

    def test_extract_text_from_pdf_path(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as t:
            t.write(b"%PDF-1.4 fake")
            pdf_path = t.name
        try:
            result = IngestResume().extract_text_from_pdf(pdf_path)
            self.assertEqual(result, "# PDF Resume\n\nSecond page body.")
            self.reader_class.assert_called_once_with(pdf_path)
            self.assertTrue(os.path.exists(pdf_path))
        finally:
            os.unlink(pdf_path)

    def test_dispatch_pdf_by_resume_name(self):
        ingest = IngestResume(resume=b"%PDF-1.4", resume_name="foo.pdf")
        with patch.object(
            IngestResume, "extract_text_from_pdf", return_value="md"
        ) as pdf_mock, patch.object(
            IngestResume, "extract_text_from_docx"
        ) as docx_mock:
            out = ingest._extract_text(ingest.resume, ingest.resume_name)
        self.assertEqual(out, "md")
        pdf_mock.assert_called_once()
        docx_mock.assert_not_called()

    def test_dispatch_docx_default(self):
        ingest = IngestResume(resume=b"fake", resume_name="foo.docx")
        with patch.object(
            IngestResume, "extract_text_from_docx", return_value="md"
        ) as docx_mock, patch.object(
            IngestResume, "extract_text_from_pdf"
        ) as pdf_mock:
            out = ingest._extract_text(ingest.resume, ingest.resume_name)
        self.assertEqual(out, "md")
        docx_mock.assert_called_once()
        pdf_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
