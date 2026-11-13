"""Unit tests for the corpus health scanner.

The scanner is exercised on synthetic PDFs so the tests never depend on the
exported corpus: a dark page, a light page, and a broken symlink are enough to
cover the classification and the symlink rules.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import diagnose_corpus_health as dch  # noqa: E402

try:
    import pikepdf
except ImportError:  # pragma: no cover - environment without pikepdf
    pikepdf = None


DARK = (
    b"q 0.75 0 0 -0.75 0 2400 cm 0.003921569 g 0 0 m 1600 0 l 1600 3200 l 0 3200 l\n"
    b"h\nf\nQ\n"
)
LIGHT = (
    b"q 1 0 0 1 0 0 cm 1 g 0 0 m 600 0 l 600 800 l 0 800 l h f Q\n"
)


@unittest.skipIf(pikepdf is None, "pikepdf is required")
class ScannerTests(unittest.TestCase):
    """Page statistics, defect classification and symlink rules."""

    def _pdf(self, path: Path, content: bytes) -> None:
        with pikepdf.Pdf.new() as pdf:
            page = pdf.add_blank_page(page_size=(600, 800))
            page.obj["/Contents"] = pdf.make_stream(content)
            pdf.save(path)

    def test_dark_page_is_reported_as_black_canvas(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dark.pdf"
            self._pdf(path, DARK)
            scan = dch.scan_pdf(path, deep=True)
            self.assertEqual(scan["page_count"], 1)
            self.assertLess(scan["pages"][0]["mean"], 30.0)
            findings = dch.classify(path, "original", scan, {"source_pages": 1,
                                                            "source_media_pdfs": 0}, None)
            codes = {finding["code"] for finding in findings}
            self.assertIn("DEFECT_BLACK_CANVAS", codes)

    def test_light_page_has_no_defect(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "light.pdf"
            self._pdf(path, LIGHT)
            scan = dch.scan_pdf(path, deep=True)
            self.assertGreater(scan["pages"][0]["mean"], 200.0)
            findings = dch.classify(path, "original", scan, {"source_pages": 0,
                                                            "source_media_pdfs": 0}, None)
            self.assertEqual(findings, [])

    def test_uniform_page_over_a_source_page_is_a_blank_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "light.pdf"
            self._pdf(path, LIGHT)
            scan = dch.scan_pdf(path, deep=True)
            findings = dch.classify(path, "original", scan, {"source_pages": 1,
                                                            "source_media_pdfs": 0}, None)
            codes = {finding["code"] for finding in findings}
            self.assertIn("DEFECT_BLANK_RENDER_CANDIDATE", codes)
            self.assertNotIn("DEFECT_BLACK_CANVAS", codes)

    def test_symlink_facts_flag_broken_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            link = root / "alias.pdf"
            link.symlink_to(root / "missing.pdf")
            facts = dch._symlink_facts(link)
            self.assertTrue(facts["is_symlink"])
            self.assertTrue(facts["broken"])

    def test_symlink_facts_flag_escapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = Path(temporary).parent / "outside-target.pdf"
            outside.write_bytes(b"%PDF-1.7\n")
            link = root / "alias.pdf"
            link.symlink_to(outside)
            try:
                facts = dch._symlink_facts(link)
                self.assertTrue(facts["is_symlink"])
                self.assertTrue(facts["outside"])
            finally:
                outside.unlink(missing_ok=True)

    def test_page_color_is_rendered_as_hex(self):
        self.assertEqual(dch._hex_argb(4278255873), "#010101")
        self.assertEqual(dch._hex_argb(4294769916), "#fcfcfc")


if __name__ == "__main__":
    unittest.main()
