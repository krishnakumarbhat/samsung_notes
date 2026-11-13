"""Regression checks for changed-note selection and incomplete output rejection."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import incremental_export as ie


class IncrementalTests(unittest.TestCase):
    """Exercise recorded timestamps, safety and independent PDF validation."""

    def test_seed_does_not_overwrite_exported_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "old.pdf"
            pdf.write_bytes(b"%PDF- old")
            tracker = root / "tracker.sqlite"
            with sqlite3.connect(tracker) as c:
                c.execute("CREATE TABLE notes(uuid,last_modified,title,folder_path,host_file_path,sha256,export_status)")
                c.execute("INSERT INTO notes VALUES('u',100,'T','F',?,'h','SUCCESS')", (str(pdf),))
            with patch.multiple(ie, STATE=root / "state.sqlite", TRACKER=tracker):
                c = ie.open_state()
                c.execute("UPDATE exports SET modified=200 WHERE uuid='u'")
                c.commit()
                c.close()
                c = ie.open_state()
                self.assertEqual(c.execute("SELECT modified FROM exports").fetchone()[0], 200)
                c.close()

    def test_traversal_is_not_a_path_component(self):
        self.assertEqual(ie.safe_component(".."), "Untitled")
        self.assertNotIn("/", ie.safe_component("a/b"))

    def test_pdf_page_count_and_truncation(self):
        import pikepdf
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "valid.pdf"
            pdf = pikepdf.Pdf.new()
            pdf.add_blank_page()
            pdf.save(path)
            self.assertEqual(ie.verify(path, 1), 1)
            with self.assertRaises(ValueError):
                ie.verify(path, 2)
            path.write_bytes(path.read_bytes()[:-50])
            with self.assertRaises(ValueError):
                ie.verify(path)

    def test_integrity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            source = work / "source"
            source.mkdir()
            with patch.object(ie, "execute", return_value="4 visible page(s)\nIntegrity pages: 3 matched, 1 mismatched, 0 unavailable"):
                with self.assertRaisesRegex(ValueError, "inconsistent"):
                    ie.render(source, work)

    def test_blank_render_is_rejected_but_ink_passes(self):
        import pikepdf
        with tempfile.TemporaryDirectory() as tmp:
            blank = Path(tmp) / "blank.pdf"
            pdf = pikepdf.Pdf.new()
            pdf.add_blank_page()
            pdf.save(blank)
            with self.assertRaisesRegex(ValueError, "blank"):
                ie.content_pages(blank, 1)
            self.assertEqual(ie.verify(blank, 1), 1)  # structural check alone is insufficient

    def test_only_source_confirmed_empty_notes_are_cached_as_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "wdoc"
            source.mkdir()
            archive = Path(tmp) / "source.sdocx"
            with patch.object(ie, "probe_archive", return_value=(1, [{"strokes": 0}], [])), \
                 patch("page_delta._note_layout_sensitive", return_value=False), \
                 patch.object(ie, "embedded_pdfs", return_value=[]):
                self.assertEqual(ie.source_is_empty(source, archive), (True, 1, 0))
            with patch.object(ie, "probe_archive", return_value=(1, [{"strokes": 3}], [])), \
                 patch("page_delta._note_layout_sensitive", return_value=False), \
                 patch.object(ie, "embedded_pdfs", return_value=[]):
                self.assertEqual(ie.source_is_empty(source, archive), (False, 1, 3))
            with patch.object(ie, "probe_archive", return_value=(1, [{"strokes": 0}], [])), \
                 patch("page_delta._note_layout_sensitive", return_value=False), \
                 patch.object(ie, "embedded_pdfs", return_value=[source / "paper.pdf"]):
                self.assertEqual(ie.source_is_empty(source, archive), (False, 1, 0))


if __name__ == "__main__":
    unittest.main()
