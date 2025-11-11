import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from job_hunting.lib.services.ingest_resume import IngestResume


class TestIngestBlobCleanup(unittest.TestCase):
    """Tests focused on temp file cleanup during blob ingestion, especially error cases."""

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_temp_file_cleanup_on_docx_parser_init_error(self, mock_docx_parser_class):
        """Test temp file cleanup when DocxParser.__init__ raises an exception."""
        # Setup mock to raise during initialization
        mock_docx_parser_class.side_effect = Exception("DocxParser init failed")

        test_blob = b"fake docx content"
        ingest_service = IngestResume()
        
        # Track temp files created
        temp_files_created = []
        original_named_temp_file = tempfile.NamedTemporaryFile
        
        def track_temp_file(*args, **kwargs):
            temp_file = original_named_temp_file(*args, **kwargs)
            temp_files_created.append(temp_file.name)
            return temp_file
        
        with patch('tempfile.NamedTemporaryFile', side_effect=track_temp_file):
            with self.assertRaises(Exception):
                ingest_service.extract_text_from_docx(test_blob)
        
        # Verify temp file was cleaned up
        for temp_path in temp_files_created:
            self.assertFalse(os.path.exists(temp_path), 
                           f"Temp file {temp_path} was not cleaned up after DocxParser init error")

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_temp_file_cleanup_on_to_markdown_error(self, mock_docx_parser_class):
        """Test temp file cleanup when to_markdown() raises an exception."""
        # Setup mock parser to fail on to_markdown
        mock_parser = MagicMock()
        mock_parser.to_markdown.side_effect = RuntimeError("Markdown conversion failed")
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
            with self.assertRaises(RuntimeError):
                ingest_service.extract_text_from_docx(test_blob)
        
        # Verify temp file was cleaned up
        for temp_path in temp_files_created:
            self.assertFalse(os.path.exists(temp_path), 
                           f"Temp file {temp_path} was not cleaned up after to_markdown error")

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    @patch('os.unlink')
    def test_temp_file_cleanup_handles_unlink_error(self, mock_unlink, mock_docx_parser_class):
        """Test that unlink errors during cleanup don't propagate."""
        # Setup mock parser to succeed
        mock_parser = MagicMock()
        mock_parser.to_markdown.return_value = "test markdown"
        mock_parser.to_html.return_value.value = "test html"
        mock_docx_parser_class.return_value = mock_parser
        
        # Setup unlink to fail
        mock_unlink.side_effect = OSError("Permission denied")

        test_blob = b"fake docx content"
        ingest_service = IngestResume()
        
        # Should not raise exception despite unlink failure
        result = ingest_service.extract_text_from_docx(test_blob)
        
        # Verify result is still returned
        self.assertEqual(result, "test markdown")
        
        # Verify unlink was attempted
        mock_unlink.assert_called()

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_multiple_temp_files_all_cleaned_up(self, mock_docx_parser_class):
        """Test cleanup when multiple temp files might be created in sequence."""
        # Setup mock parser
        mock_parser = MagicMock()
        mock_parser.to_markdown.return_value = "test markdown"
        mock_parser.to_html.return_value.value = "test html"
        mock_docx_parser_class.return_value = mock_parser

        ingest_service = IngestResume()
        
        # Track all temp files created across multiple calls
        all_temp_files = []
        original_named_temp_file = tempfile.NamedTemporaryFile
        
        def track_temp_file(*args, **kwargs):
            temp_file = original_named_temp_file(*args, **kwargs)
            all_temp_files.append(temp_file.name)
            return temp_file
        
        with patch('tempfile.NamedTemporaryFile', side_effect=track_temp_file):
            # Process multiple blobs
            for i in range(3):
                test_blob = f"fake docx content {i}".encode()
                result = ingest_service.extract_text_from_docx(test_blob)
                self.assertEqual(result, "test markdown")
        
        # Verify all temp files were cleaned up
        for temp_path in all_temp_files:
            self.assertFalse(os.path.exists(temp_path), 
                           f"Temp file {temp_path} was not cleaned up")
        
        # Should have created 3 temp files
        self.assertEqual(len(all_temp_files), 3)

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_temp_file_cleanup_with_file_write_error(self, mock_docx_parser_class):
        """Test cleanup when temp file writing fails."""
        # Setup mock parser (won't be reached due to write error)
        mock_parser = MagicMock()
        mock_docx_parser_class.return_value = mock_parser

        test_blob = b"fake docx content"
        ingest_service = IngestResume()
        
        # Track temp files
        temp_files_created = []
        original_named_temp_file = tempfile.NamedTemporaryFile
        
        def track_temp_file(*args, **kwargs):
            temp_file = original_named_temp_file(*args, **kwargs)
            temp_files_created.append(temp_file.name)
            # Mock write to fail
            temp_file.write = MagicMock(side_effect=OSError("Disk full"))
            return temp_file
        
        with patch('tempfile.NamedTemporaryFile', side_effect=track_temp_file):
            with self.assertRaises(OSError):
                ingest_service.extract_text_from_docx(test_blob)
        
        # Verify temp file was cleaned up even after write error
        for temp_path in temp_files_created:
            self.assertFalse(os.path.exists(temp_path), 
                           f"Temp file {temp_path} was not cleaned up after write error")
