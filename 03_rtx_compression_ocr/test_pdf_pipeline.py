"""Unit checks for text bypass, invisible OCR text, and lossless compression."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import paddle_ocr_pdf_batch as pipeline

import pikepdf

from paddle_ocr_pdf_batch import (
    add_invisible_text,
    compress_pdf_losslessly,
    fitz,
    page_has_native_text,
    process_pdf,
)


class NativeTextPage:
    """Small fake page for testing the selectable-text threshold."""

    def __init__(self, text: str):
        self.text = text

    def get_text(self, _kind: str) -> str:
        return self.text


class PdfPipelineTests(unittest.TestCase):
    """Verify OCR bypass rules and ensure OCR-layer/compression are lossless."""

    def test_native_page_bypasses_only_at_fifty_clean_characters(self):
        self.assertFalse(page_has_native_text(NativeTextPage("x" * 49)))
        self.assertTrue(page_has_native_text(NativeTextPage("x" * 50)))
        self.assertTrue(page_has_native_text(NativeTextPage("x" * 25 + "\x00" * 50 + "y" * 25)))
        # A spaced-out title page must not count as text rich.
        self.assertFalse(page_has_native_text(NativeTextPage("x " * 30)))

    def test_rotated_ocr_boxes_map_back_onto_the_upright_page(self):
        """Quarter-turn variants are inverted before invisible text is placed."""
        upright = (100, 200, 3)  # H=100, W=200
        self.assertEqual(pipeline.unrotate_polygon([[3, 4]], 0, upright), [[3.0, 4.0]])
        # Clockwise: (x, y) -> (y, H-1-x)
        self.assertEqual(pipeline.unrotate_polygon([[3, 4]], -1, upright), [[4.0, 96.0]])
        # Counter-clockwise: (x, y) -> (W-1-y, x)
        self.assertEqual(pipeline.unrotate_polygon([[3, 4]], 1, upright), [[195.0, 3.0]])

    def test_invisible_text_is_searchable_without_visual_change(self):
        doc = fitz.open()
        page = doc.new_page(width=300, height=200)
        page.draw_rect(fitz.Rect(20, 20, 280, 180), color=(0.1, 0.2, 0.8), width=2)
        page.insert_text((30, 55), "Existing visible note ink", fontsize=14)
        before = page.get_pixmap(alpha=False).samples
        self.assertTrue(
            add_invisible_text(
                page, fitz.Rect(30, 90, 270, 130), "gradient descent learning rate"
            )
        )
        after = page.get_pixmap(alpha=False).samples
        self.assertEqual(before, after)
        self.assertIn("gradient descent learning rate", page.get_text())
        doc.close()

    def test_lossless_compression_preserves_text_and_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "source.pdf", root / "compressed.pdf"
            doc = fitz.open()
            page = doc.new_page(width=300, height=200)
            page.draw_rect(fitz.Rect(10, 10, 290, 190), color=(0, 0, 0), width=2)
            page.insert_text((24, 50), "Searchable diagram and labels", fontsize=16)
            doc.save(source)
            expected_pixels = page.get_pixmap(alpha=False).samples
            doc.close()

            compress_pdf_losslessly(source, output, expected_pages=1)

            with fitz.open(output) as compressed:
                self.assertEqual(len(compressed), 1)
                self.assertEqual(compressed[0].get_pixmap(alpha=False).samples, expected_pixels)
                self.assertIn("Searchable diagram and labels", compressed[0].get_text())
            with pikepdf.Pdf.open(output) as pdf:
                self.assertFalse(
                    [issue for issue in pdf.check_pdf_syntax() if "error" in str(issue).lower()]
                )

    def test_process_pdf_skips_ocr_on_text_rich_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "typed.pdf", root / "out" / "typed.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text(
                (72, 72),
                "This page already has more than fifty selectable characters in its native text layer.",
            )
            doc.save(source)
            doc.close()

            def forbidden_ocr():
                raise AssertionError("OCR must be bypassed for a text-rich page")

            process_pdf(source, output, forbidden_ocr, 180, 2048, 0.6, False)
            self.assertTrue(output.is_file())
            with fitz.open(output) as result:
                self.assertEqual(len(result), 1)
                self.assertIn("native text layer", result[0].get_text())

    def test_process_pdf_skips_a_featureless_monochrome_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "blank.pdf", root / "out" / "blank.pdf"
            doc = fitz.open()
            doc.new_page()
            doc.save(source)
            doc.close()

            def forbidden_ocr():
                raise AssertionError("OCR must be bypassed for a monochrome blank page")

            process_pdf(source, output, forbidden_ocr, 180, 2048, 0.6, False)
            with fitz.open(output) as result:
                self.assertEqual(len(result), 1)
                pix = result[0].get_pixmap(colorspace=fitz.csGRAY, alpha=False)
                self.assertEqual(pix.samples.count(pix.samples[0]), len(pix.samples))

    def test_identical_rendered_pages_reuse_ocr_regions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_script = root / "pipeline.py"
            fake_script.write_text("test profile")
            source, output = root / "in.pdf", root / "out.pdf"
            with fitz.open() as pdf:
                for _ in range(2):
                    page = pdf.new_page(width=200, height=200)
                    page.draw_rect(fitz.Rect(20, 20, 100, 100), color=(0, 0, 0))
                pdf.save(source)
            with patch.object(pipeline, "__file__", str(fake_script)), \
                 patch.object(pipeline, "select_best_ocr_results", return_value=([], "original", 0)) as infer:
                process_pdf(source, output, lambda: object(), 72, 256, .6, False)
            self.assertEqual(infer.call_count, 1)

    def test_failed_inference_is_not_silently_cached_as_empty(self):
        with patch.object(pipeline, "normalize_ocr_results", return_value=([], "inference error")):
            with self.assertRaisesRegex(RuntimeError, "All OCR inference passes failed"):
                pipeline.select_best_ocr_results(object(), np.zeros((8, 8, 3), dtype=np.uint8), .6)

    def test_greek_math_text_remains_selectable_and_invisible(self):
        font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font.is_file():
            self.skipTest("Unicode font unavailable")
        with fitz.open() as pdf:
            page = pdf.new_page(width=300, height=200)
            before = page.get_pixmap(alpha=False).samples
            self.assertTrue(add_invisible_text(page, fitz.Rect(20, 20, 280, 60), "α + β = ∂f"))
            self.assertIn("α + β", page.get_text())
            self.assertEqual(before, page.get_pixmap(alpha=False).samples)


if __name__ == "__main__":
    unittest.main()
