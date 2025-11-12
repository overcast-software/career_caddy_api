import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from job_hunting.lib.services.ingest_resume import IngestResume


class TestIngestResumeBlob(unittest.TestCase):
    """Unit tests for IngestResume.extract_text_from_docx with binary blob input."""

    def setUp(self):
        self.test_markdown = "# Test Resume\n\nThis is test markdown content."
        self.test_html = "<h1>Test Resume</h1><p>This is test HTML content.</p>"
        # Inject a stub Agent to avoid external API calls during tests
        class _StubResult:
            def __init__(self, output=None):
                self.output = output
            def usage(self):
                return {}
        self.stub_agent = MagicMock()
        self.stub_agent.run_sync.return_value = _StubResult(output=MagicMock())
        self._get_agent_patcher = patch('job_hunting.lib.services.ingest_resume.IngestResume.get_agent', return_value=self.stub_agent)
        self._get_agent_patcher.start()

    def tearDown(self):
        # Stop get_agent stub patcher if started
        if hasattr(self, "_get_agent_patcher"):
            self._get_agent_patcher.stop()

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_extract_text_from_blob_success(self, mock_docx_parser_class):
        """Test successful blob-based extraction with temp file cleanup."""
        # Setup mock parser
        mock_parser = MagicMock()
        mock_parser.to_markdown.return_value = self.test_markdown
        mock_parser.to_html.return_value.value = self.test_html
        mock_docx_parser_class.return_value = mock_parser

        # Create test blob
        test_blob = b"fake docx content for testing"
        
        # Create IngestResume instance
        ingest_service = IngestResume()
        
        # Track temp files created during test
        temp_files_created = []
        original_named_temp_file = tempfile.NamedTemporaryFile
        
        def track_temp_file(*args, **kwargs):
            temp_file = original_named_temp_file(*args, **kwargs)
            temp_files_created.append(temp_file.name)
            return temp_file
        
        with patch('tempfile.NamedTemporaryFile', side_effect=track_temp_file):
            # Call extract_text_from_docx with blob
            result = ingest_service.extract_text_from_docx(test_blob)
        
        # Verify result
        self.assertEqual(result, self.test_markdown)
        
        # Verify DocxParser was called with temp file path
        self.assertEqual(mock_docx_parser_class.call_count, 1)
        temp_path_used = mock_docx_parser_class.call_args[0][0]
        self.assertTrue(temp_path_used.endswith('.docx'))
        
        # Verify temp file was cleaned up
        for temp_path in temp_files_created:
            self.assertFalse(os.path.exists(temp_path), 
                           f"Temp file {temp_path} was not cleaned up")
        
        # Verify no disk artifacts created for blob input
        self.assertFalse(os.path.exists('resume.html'))
        self.assertFalse(os.path.exists('resume.md'))

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_extract_text_from_blob_parser_error_cleanup(self, mock_docx_parser_class):
        """Test temp file cleanup when DocxParser raises an exception."""
        # Setup mock parser to raise exception
        mock_parser = MagicMock()
        mock_parser.to_markdown.side_effect = RuntimeError("Parser failed")
        mock_docx_parser_class.return_value = mock_parser

        test_blob = b"fake docx content"
        ingest_service = IngestResume()
        
        # Track temp files
        temp_files_created = []
        original_named_temp_file = tempfile.NamedTemporaryFile
        
        def track_temp_file(*args, **kwargs):
            temp_file = original_named_temp_file(*args, **kwargs)
            temp_files_created.append(temp_file.name)
            return temp_file
        
        with patch('tempfile.NamedTemporaryFile', side_effect=track_temp_file):
            # Should raise RuntimeError but still clean up temp file
            with self.assertRaises(RuntimeError):
                ingest_service.extract_text_from_docx(test_blob)
        
        # Verify temp file was cleaned up even after exception
        for temp_path in temp_files_created:
            self.assertFalse(os.path.exists(temp_path), 
                           f"Temp file {temp_path} was not cleaned up after exception")

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_extract_text_from_file_like_object(self, mock_docx_parser_class):
        """Test extraction from file-like object (has read() method)."""
        # Setup mock parser
        mock_parser = MagicMock()
        mock_parser.to_markdown.return_value = self.test_markdown
        mock_parser.to_html.return_value.value = self.test_html
        mock_docx_parser_class.return_value = mock_parser

        # Create file-like object
        from io import BytesIO
        file_like = BytesIO(b"fake docx content")
        
        ingest_service = IngestResume()
        
        # Track temp files
        temp_files_created = []
        original_named_temp_file = tempfile.NamedTemporaryFile
        
        def track_temp_file(*args, **kwargs):
            temp_file = original_named_temp_file(*args, **kwargs)
            temp_files_created.append(temp_file.name)
            return temp_file
        
        with patch('tempfile.NamedTemporaryFile', side_effect=track_temp_file):
            result = ingest_service.extract_text_from_docx(file_like)
        
        # Verify result and cleanup
        self.assertEqual(result, self.test_markdown)
        for temp_path in temp_files_created:
            self.assertFalse(os.path.exists(temp_path))

    def test_extract_text_from_invalid_blob_type(self):
        """Test error handling for invalid blob types."""
        ingest_service = IngestResume()
        
        # Test with non-bytes, non-string, non-file-like object
        with self.assertRaises((AttributeError, TypeError)):
            ingest_service.extract_text_from_docx(123)  # Invalid type
