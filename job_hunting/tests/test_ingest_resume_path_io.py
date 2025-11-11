import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from job_hunting.lib.services.ingest_resume import IngestResume


class TestIngestResumePathIO(unittest.TestCase):
    """Unit tests for IngestResume.extract_text_from_docx with filesystem path input."""

    def setUp(self):
        self.test_markdown = "# Test Resume\n\nThis is test markdown content."
        self.test_html = "<h1>Test Resume</h1><p>This is test HTML content.</p>"

    def tearDown(self):
        # Clean up any test artifacts
        for filename in ['resume.html', 'resume.md']:
            if os.path.exists(filename):
                try:
                    os.unlink(filename)
                except Exception:
                    pass

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_path_ingest_writes_md_and_html_and_returns_md(self, mock_docx_parser_class):
        """Test path-based extraction writes both resume.html and resume.md and returns markdown."""
        # Setup mock parser
        mock_parser = MagicMock()
        mock_parser.to_markdown.return_value = self.test_markdown
        mock_parser.to_html.return_value.value = self.test_html
        mock_docx_parser_class.return_value = mock_parser

        # Create a real temp .docx file
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as temp_file:
            temp_file.write(b"fake docx content")
            temp_path = temp_file.name

        try:
            ingest_service = IngestResume()
            
            # Call extract_text_from_docx with path
            result = ingest_service.extract_text_from_docx(temp_path)
            
            # Verify result
            self.assertEqual(result, self.test_markdown)
            
            # Verify DocxParser was called with the correct path
            mock_docx_parser_class.assert_called_once_with(temp_path)
            
            # Verify disk artifacts are created for path input
            self.assertTrue(os.path.exists('resume.html'))
            self.assertTrue(os.path.exists('resume.md'))
            
            # Verify content of disk artifacts
            with open('resume.html', 'r') as f:
                html_content = f.read()
                self.assertEqual(html_content, self.test_html)
            
            with open('resume.md', 'r') as f:
                md_content = f.read()
                self.assertEqual(md_content, self.test_markdown)
                
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_path_ingest_html_error_does_not_write_html(self, mock_docx_parser_class):
        """Test that HTML generation errors don't prevent markdown extraction."""
        # Setup mock parser - HTML fails but markdown succeeds
        mock_parser = MagicMock()
        mock_parser.to_markdown.return_value = self.test_markdown
        mock_parser.to_html.side_effect = Exception("HTML conversion failed")
        mock_docx_parser_class.return_value = mock_parser

        # Create a real temp .docx file
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as temp_file:
            temp_file.write(b"fake docx content")
            temp_path = temp_file.name

        try:
            ingest_service = IngestResume()
            
            # Should still succeed despite HTML error
            result = ingest_service.extract_text_from_docx(temp_path)
            
            # Verify markdown result
            self.assertEqual(result, self.test_markdown)
            
            # Verify markdown file is still created
            self.assertTrue(os.path.exists('resume.md'))
            with open('resume.md', 'r') as f:
                self.assertEqual(f.read(), self.test_markdown)
            
            # HTML file should not exist due to error
            self.assertFalse(os.path.exists('resume.html'))
                
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_path_ingest_invalid_path_raises(self):
        """Test error handling for non-existent file path."""
        ingest_service = IngestResume()
        
        with self.assertRaises(ValueError) as cm:
            ingest_service.extract_text_from_docx("/nonexistent/file.docx")
        
        self.assertIn("File not found", str(cm.exception))

    def test_extract_text_from_non_docx_path(self):
        """Test error handling for non-.docx file extension."""
        # Create a temp file with wrong extension
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as temp_file:
            temp_file.write(b"not a docx")
            temp_path = temp_file.name

        try:
            ingest_service = IngestResume()
            
            with self.assertRaises(ValueError) as cm:
                ingest_service.extract_text_from_docx(temp_path)
            
            self.assertIn("Only .docx files are supported", str(cm.exception))
                
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @patch('job_hunting.lib.services.ingest_resume.DocxParser')
    def test_extract_text_from_path_markdown_error(self, mock_docx_parser_class):
        """Test error handling when markdown conversion fails."""
        # Setup mock parser to fail on markdown conversion
        mock_parser = MagicMock()
        mock_parser.to_markdown.side_effect = Exception("Markdown conversion failed")
        mock_docx_parser_class.return_value = mock_parser

        # Create a real temp .docx file
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as temp_file:
            temp_file.write(b"fake docx content")
            temp_path = temp_file.name

        try:
            ingest_service = IngestResume()
            
            with self.assertRaises(RuntimeError) as cm:
                ingest_service.extract_text_from_docx(temp_path)
            
            self.assertIn("Failed to convert .docx to markdown", str(cm.exception))
                
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
