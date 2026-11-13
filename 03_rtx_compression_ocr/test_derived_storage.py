"""Publication safety: derived placeholders never expose originals to write-through."""
from __future__ import annotations
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import fitz
import derive_corpus as derived


class DerivedStorageTests(unittest.TestCase):
    """Use small synthetic PDFs and no OCR/network to verify immutable source behavior."""

    def make_pdf(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((40, 70), text)
            pdf.save(path)

    def test_publication_replaces_symlink_not_original(self):
        with tempfile.TemporaryDirectory() as temporary:
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
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "a.pdf", Path(temporary) / "b.pdf"
            self.make_pdf(source, "Original ink")
            self.make_pdf(output, "Changed ink")
            with self.assertRaisesRegex(ValueError, "visible content"):
                derived.verify_pair(source, output)

    def test_guard_failure_retains_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
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


if __name__ == "__main__":
    unittest.main()
