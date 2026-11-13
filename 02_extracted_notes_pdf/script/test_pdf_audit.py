"""Tests for read-only PDF validation and recorded page-count checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_and_repair_blanks import fitz, inspect_pdf
import pikepdf


class PdfAuditTests(unittest.TestCase):
    """Cover size, page-count and pure-white render checks."""

    def test_reports_missing_tiny_and_white_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = inspect_pdf(root / "missing.pdf")
            self.assertIn("missing", missing["flags"])
            tiny = root / "tiny.pdf"
            pdf = pikepdf.Pdf.new()
            pdf.add_blank_page()
            pdf.save(tiny)
            result = inspect_pdf(tiny, expected_pages=1, wdoc_page_files=1)
            self.assertIn("file-under-10KiB", result["flags"])
            self.assertEqual(result["blank_pages"], [1])

    def test_reports_state_and_wdoc_page_count_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.pdf"
            document = fitz.open()
            for _ in range(20):
                page = document.new_page()
                page.insert_text((30, 40), "This fixture contains visible sample text.")
            document.save(path)
            document.close()
            state_mismatch = inspect_pdf(path, expected_pages=2, wdoc_page_files=20)
            self.assertIn("incremental-state-page-count-mismatch", state_mismatch["flags"])
            self.assertNotIn("raw-wdoc-page-file-count-mismatch", state_mismatch["flags"])

    def test_records_raw_wdoc_page_file_count_as_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.pdf"
            document = fitz.open()
            for _ in range(2):
                page = document.new_page()
                page.insert_text((30, 40), "Visible text from this sample page.")
            document.save(path)
            document.close()
            result = inspect_pdf(path, expected_pages=2, wdoc_page_files=3)
            self.assertTrue(result["raw_wdoc_count_differs"])
            self.assertNotIn("raw-wdoc-page-file-count-mismatch", result["flags"])


if __name__ == "__main__":
    unittest.main()
