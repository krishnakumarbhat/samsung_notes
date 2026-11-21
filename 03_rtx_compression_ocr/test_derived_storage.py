"""Publication safety: derived placeholders never expose originals to write-through."""
from __future__ import annotations
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pymupdf_compat import fitz
import derive_corpus as derived
import pikepdf
from pdf_compress import compress


class DerivedStorageTests(unittest.TestCase):
    """Use small synthetic PDFs and no OCR/network to verify immutable source behavior."""

    def make_pdf(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((40, 70), text)
            pdf.save(path)

    def test_publication_replaces_symlink_not_original(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            original, compressed = root / "original", root / "compressed"
            raw = original / "maths/note.pdf"
            self.make_pdf(raw, "A long native text sentence that should bypass all OCR work entirely.")
            before = hashlib.sha256(raw.read_bytes()).hexdigest()
            target = compressed / "maths/note.pdf"
            target.parent.mkdir(parents=True)
            target.symlink_to(os.path.relpath(raw, target.parent))
            with patch.object(derived, "ORIGINAL", original), patch.object(derived, "COMPRESSED", compressed), \
                 patch.object(derived, "guard"):
                result = derived.derive_file(raw, ocr=False)
            self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), before)
            self.assertTrue(target.is_file())
            self.assertFalse(target.is_symlink())
            self.assertEqual(result["pages"], 1)
            self.assertEqual(result["status"], "searchable")

    def test_visual_difference_is_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            source, output = Path(temporary) / "a.pdf", Path(temporary) / "b.pdf"
            self.make_pdf(source, "Original ink")
            self.make_pdf(output, "Changed ink")
            with self.assertRaisesRegex(ValueError, "visible content"):
                derived.verify_pair(source, output)

    def test_pixel_comparison_accepts_only_tiny_rasterizer_noise(self):
        before = bytearray([120] * (100 * 3))
        after = bytearray(before)
        after[0] = 121
        self.assertTrue(derived._visible_render_matches(
            SimpleNamespace(width=10, height=10, samples=bytes(before)),
            SimpleNamespace(width=10, height=10, samples=bytes(after)),
        ))
        after = bytearray(before)
        for offset in range(0, 30, 3):
            after[offset] = 255
        self.assertFalse(derived._visible_render_matches(
            SimpleNamespace(width=10, height=10, samples=bytes(before)),
            SimpleNamespace(width=10, height=10, samples=bytes(after)),
        ))

    def test_light_canvas_normalization_is_staged_and_preserves_source(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            source, work = root / "dark.pdf", root / "scratch"
            with fitz.open() as pdf:
                page = pdf.new_page(width=400, height=500)
                page.draw_rect(page.rect, color=(0.08, 0.08, 0.08),
                               fill=(0.08, 0.08, 0.08), overlay=True)
                page.insert_text((50, 80), "white canvas test", color=(1, 1, 1))
                pdf.save(source)
            before = source.read_bytes()
            with patch.object(derived, "HISTORY", root / "history"):
                normalized, warnings = derived.normalize_light_canvas(source, work)
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(normalized.is_file())
            self.assertNotEqual(normalized, source)
            self.assertTrue(any("light-canvas-normalized" in item for item in warnings))
            with fitz.open(normalized) as pdf:
                self.assertGreater(pdf[0].get_pixmap().samples.count(255), 0)

    def test_guard_failure_retains_placeholder(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            raw = root / "original/note.pdf"
            self.make_pdf(raw, "Native note")
            target = root / "compressed/note.pdf"
            target.parent.mkdir()
            target.symlink_to(os.path.relpath(raw, target.parent))
            with patch.object(derived, "ORIGINAL", root / "original"), \
                 patch.object(derived, "COMPRESSED", root / "compressed"), \
                 patch.object(derived, "guard", side_effect=RuntimeError("Disk budget")):
                with self.assertRaisesRegex(RuntimeError, "Disk budget"):
                    derived.derive_file(raw)
            self.assertTrue(target.is_symlink())
            self.assertTrue(raw.is_file())

    def test_differing_previous_pdf_is_archived_before_atomic_replacement(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            source = root / "original/note.pdf"
            target = root / "compressed/note.pdf"
            self.make_pdf(source, "Current note content")
            self.make_pdf(target, "Previous unique note content")
            old_bytes = target.read_bytes()
            with patch.object(derived, "ORIGINAL", root / "original"), \
                 patch.object(derived, "COMPRESSED", root / "compressed"), \
                 patch.object(derived, "HISTORY", root / "history"), \
                 patch.object(derived, "guard"):
                derived.derive_file(source, target=target, ocr=False)
            archived = list((root / "history/preserved_originals/replaced_exports").rglob("*.pdf"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), old_bytes)
            self.assertNotEqual(target.read_bytes(), old_bytes)

    def test_links_outlines_search_and_labels_survive_without_staging(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            source = root / "original/note.pdf"
            source.parent.mkdir()
            with fitz.open() as pdf:
                for text in ("Index: gradient descent", "gradient descent learning rate"):
                    page = pdf.new_page()
                    page.insert_text((40, 70), text)
                pdf[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(30, 50, 180, 90),
                                    "page": 1, "to": fitz.Point(0, 60)})
                pdf[1].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(30, 50, 180, 90),
                                    "uri": "https://example.org/concept"})
                pdf.set_toc([[1, "Index", 1], [1, "Concept", 2]])
                pdf.set_page_labels([{"startpage": 0, "prefix": "N-", "style": "D"}])
                pdf.save(source)
            target = root / "compressed/note.pdf"
            with patch.object(derived, "COMPRESSED", root / "compressed"), patch.object(derived, "guard"):
                result = derived.derive_file(source, target=target)
            self.assertLessEqual(target.stat().st_size, source.stat().st_size)
            derived.verify_pair(source, target)
            self.assertEqual(result["storage"], "standalone")
            source.unlink()
            with fitz.open(target) as pdf:
                self.assertEqual(pdf[0].get_links()[0]["page"], 1)
                self.assertTrue(pdf[1].search_for("gradient descent"))
                self.assertEqual(pdf.get_toc()[1][1], "Concept")

    def test_missing_link_rejected_even_with_identical_pixels(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            source, output = Path(temporary) / "a.pdf", Path(temporary) / "b.pdf"
            self.make_pdf(output, "Same visible text")
            with fitz.open(output) as pdf:
                pdf[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(10, 10, 20, 20),
                                    "uri": "https://example.org"})
                pdf.save(source)
            with self.assertRaisesRegex(ValueError, "hyperlinks"):
                derived.verify_pair(source, output)

    def test_optimizer_error_falls_back_to_real_source_bytes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            source, target = root / "original/a.pdf", root / "compressed/a.pdf"
            self.make_pdf(source, "Fallback remains searchable")
            with patch.object(derived, "COMPRESSED", root / "compressed"), \
                 patch.object(derived, "guard"), patch("pdf_compress.compress", side_effect=ValueError("unsafe")):
                result = derived.derive_file(source, target=target)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            self.assertTrue(result["warnings"])

    def test_dedup_never_merges_identical_bytes_with_different_geometry(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            source, target = Path(temporary) / "a.pdf", Path(temporary) / "b.pdf"
            with pikepdf.new() as pdf:
                page = pdf.add_blank_page()
                resources = pikepdf.Dictionary()
                images = pikepdf.Dictionary()
                for name, width, height in (("/A", 1, 2), ("/B", 2, 1)):
                    image = pdf.make_stream(b"\x00\xff")
                    image["/Type"] = pikepdf.Name("/XObject")
                    image["/Subtype"] = pikepdf.Name("/Image")
                    image["/Width"], image["/Height"] = width, height
                    image["/BitsPerComponent"] = 8
                    image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
                    images[name] = image
                resources["/XObject"] = images
                page.obj["/Resources"] = resources
                pdf.save(source)
            report = compress(source, target, lossless=True)
            self.assertEqual(report.images_deduplicated, 0)
            with pikepdf.open(target) as pdf:
                images = pdf.pages[0].Resources.XObject
                self.assertNotEqual(images.A.objgen, images.B.objgen)

    def test_lossless_image_reduction_preserves_pixels_and_text(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            source, target = Path(temporary) / "a.pdf", Path(temporary) / "b.pdf"
            self.make_pdf(source, "Existing searchable image caption")
            raw = bytes(channel for y in range(128) for x in range(128)
                        for channel in [(x * 7 + y * 13) % 256] * 3)
            draft = source.with_suffix(".draft.pdf")
            with pikepdf.open(source) as pdf:
                image = pdf.make_stream(raw)
                for key, value in {"/Type": pikepdf.Name("/XObject"),
                                   "/Subtype": pikepdf.Name("/Image"),
                                   "/ColorSpace": pikepdf.Name("/DeviceRGB"),
                                   "/Width": 128, "/Height": 128,
                                   "/BitsPerComponent": 8}.items():
                    image[key] = value
                pdf.pages[0].Resources["/XObject"] = pikepdf.Dictionary({"/Im": image})
                pdf.pages[0].contents_add(b"q 128 0 0 128 0 0 cm /Im Do Q")
                pdf.save(draft)
            draft.replace(source)
            report = compress(source, target, lossless=True)
            self.assertEqual(report.images_gray, 1)
            derived.verify_pair(source, target)
            self.assertLess(target.stat().st_size, source.stat().st_size)

    def test_signed_document_keeps_exact_bytes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            source, target = root / "original/a.pdf", root / "compressed/a.pdf"
            self.make_pdf(source, "Do not invalidate a signature")
            draft = source.with_suffix(".draft.pdf")
            with pikepdf.open(source) as pdf:
                pdf.Root["/Perms"] = pikepdf.Dictionary()
                pdf.save(draft)
            draft.replace(source)
            with patch.object(derived, "COMPRESSED", root / "compressed"), \
                 patch.object(derived, "guard"), patch("pdf_compress.compress") as optimizer:
                derived.derive_file(source, target=target)
            optimizer.assert_not_called()
            self.assertEqual(source.read_bytes(), target.read_bytes())


if __name__ == "__main__":
    unittest.main()
