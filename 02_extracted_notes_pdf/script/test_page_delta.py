"""Synthetic tests for page-level cache reuse, corruption and atomic state."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pikepdf

import page_delta as pd


def _uuid(number: int) -> str:
    """Return a deterministic 36-character UUID-shaped page identifier."""
    return f"00000000-0000-0000-0000-{number:012x}"


def _write_source(root: Path, pages: dict[str, bytes]) -> None:
    """Create a minimal WDoc-like source with an authoritative page manifest."""
    root.mkdir(parents=True, exist_ok=True)
    for page_id, data in pages.items():
        (root / f"{page_id}.page").write_bytes(data)
    manifest = b"\0" * 32 + struct.pack("<H", len(pages))
    for page_id in pages:
        units = page_id.encode("utf-16-le")
        manifest += struct.pack("<H", len(units) // 2) + units + b"\0" * 32
    (root / "pageIdInfo.dat").write_bytes(manifest)


def _rewrite_manifest(root: Path, order: list[str]) -> None:
    """Rewrite only the page ordering manifest (simulates insertion/reorder)."""
    manifest = b"\0" * 32 + struct.pack("<H", len(order))
    for page_id in order:
        units = page_id.encode("utf-16-le")
        manifest += struct.pack("<H", len(units) // 2) + units + b"\0" * 32
    (root / "pageIdInfo.dat").write_bytes(manifest)


def _frame(kind: int, fields: bytes = b"", flexible: bytes = b"") -> bytes:
    """Build one minimal Samsung frame understood by page_delta's reader."""
    header = struct.pack("<hI", kind, 8 + len(fields) + 4) + b"\0" + bytes([len(fields)]) + fields
    return struct.pack("<I", len(header) + len(flexible) + 4) + header + flexible


def _note_with_body(text: str) -> bytes:
    """Build a minimal note.note carrying the given document body text."""
    units = text.encode("utf-16-le")
    common = struct.pack("<I", len(units) // 2) + units
    body = _frame(0) + _frame(6) + _frame(7, b"\x01", struct.pack("<I", len(common)) + common)
    note = struct.pack("<I", 0) + b"\0\0" + struct.pack("<I", 0)
    note += struct.pack("<H", 0) + struct.pack("<I", 0) + struct.pack("<q", 0) * 2
    note += struct.pack("<I", 0) * 5 + struct.pack("<I", 0)
    note += struct.pack("<I", len(body)) + body
    return struct.pack("<I", len(note)) + note[4:]


class PageDeltaTests(unittest.TestCase):
    """Exercise insertion, edit, corruption and failure-safe publication."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.source = self.root / "wdoc"
        self.calls: list[str] = []
        self.patches = [
            patch.object(pd, "PAGE_CACHE_DIR", self.cache),
            patch.object(pd, "PROJECT_ROOT", self.root),
            patch.object(pd, "FREE_FLOOR_BYTES", 0),
            patch.object(pd, "PROJECT_CAP_BYTES", 10**12),
            patch.object(pd, "PAGE_CACHE_LIMIT_BYTES", 10**12),
            patch.object(pd, "_probe_archive", self._probe),
            patch.object(pd, "_render_page_pdf", self._render),
            patch.object(pd, "_content_pages", lambda *args: 0),
            patch.object(pd, "_renderer_config", lambda: {"test": "renderer"}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _probe(self, archive: Path):
        """Return the manifest count with one-page CLI metadata."""
        ids = [page_id for page_id, _hash in pd._read_page_manifest(archive)]
        info = [{"index": i, "template": "blank", "strokes": 1}
                for i in range(len(ids))]
        return len(ids), info, []

    def _render(self, archive, index, page_id, output, warnings):
        """Record one page render and emit a valid single-page PDF."""
        self.calls.append(page_id)
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page()
        pdf.save(output)

    def _compile(self):
        return pd.compile_note(self.source, self.root / "work")

    def test_insertion_and_edit_recompile_only_changed_page(self) -> None:
        a, b, c, d = (_uuid(i) for i in range(1, 5))
        _write_source(self.source, {a: b"A", b: b"B", c: b"C"})
        output, pages, warnings = self._compile()
        self.assertEqual(pages, 3)
        self.assertEqual(self.calls, [a, b, c])
        self.assertTrue(output.is_file())
        self.assertFalse(any("fallback" in warning for warning in warnings))

        (self.source / f"{d}.page").write_bytes(b"D")
        _rewrite_manifest(self.source, [a, b, d, c])
        self.calls.clear()
        output, pages, _ = self._compile()
        self.assertEqual(pages, 4)
        self.assertEqual(self.calls, [d])
        with pikepdf.open(output) as pdf:
            self.assertEqual(len(pdf.pages), 4)

        (self.source / f"{b}.page").write_bytes(b"B-edited")
        self.calls.clear()
        _output, pages, _ = self._compile()
        self.assertEqual(pages, 4)
        self.assertEqual(self.calls, [b])

        _rewrite_manifest(self.source, [c, a, b, d])
        self.calls.clear()
        _output, pages, _ = self._compile()
        self.assertEqual(pages, 4)
        self.assertEqual(self.calls, [])

    def _entry_for(self, page_id: str) -> Path:
        """Find the cache entry directory for a page UUID."""
        for path in self.cache.rglob("entry.json"):
            meta = json.loads(path.read_text())
            if meta.get("page_uuid") == page_id:
                return path.parent
        self.fail(f"cache entry not found for {page_id}")

    def test_corrupt_or_missing_cache_recompiles_only_that_page(self) -> None:
        a, b = _uuid(11), _uuid(12)
        _write_source(self.source, {a: b"A", b: b"B"})
        self._compile()
        entry = self._entry_for(b)
        (entry / "page.pdf").write_bytes(b"not a pdf")
        self.calls.clear()
        self._compile()
        self.assertEqual(self.calls, [b])

        entry = self._entry_for(b)
        (entry / "entry.json").write_text("{broken")
        self.calls.clear()
        self._compile()
        self.assertEqual(self.calls, [b])

        entry = self._entry_for(b)
        (entry / "page.pdf").unlink()
        self.calls.clear()
        self._compile()
        self.assertEqual(self.calls, [b])
        self.assertFalse(list(self.cache.rglob("*.tmp")))

    def test_atomic_publication_keeps_old_file_on_failure(self) -> None:
        target = self.root / "atomic.json"
        target.write_bytes(b"old")
        with patch.object(pd.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pd._atomic_write_bytes(target, b"new")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(list(target.parent.glob(".atomic.json.*.tmp")))

    def test_failed_splice_does_not_publish_note_manifest(self) -> None:
        a, b = _uuid(21), _uuid(22)
        _write_source(self.source, {a: b"A", b: b"B"})
        fake = self.root / "fallback.pdf"
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page()
        pdf.save(fake)
        with patch.object(pd, "_splice_pages", side_effect=RuntimeError("splice")), \
             patch.object(pd, "_fallback", return_value=(fake, 1, ["fallback"]) ) as fallback:
            output, pages, warnings = self._compile()
        self.assertEqual((output, pages), (fake, 1))
        fallback.assert_called_once()
        self.assertIn("splice", fallback.call_args.args[3])
        self.assertFalse(list((self.cache / pd.CACHE_SCHEMA / "notes").glob("*.json")))

    def test_missing_manifest_and_embedded_pdf_fail_closed_to_full_render(self) -> None:
        a = _uuid(31)
        _write_source(self.source, {a: b"A"})
        (self.source / "pageIdInfo.dat").unlink()
        fake = self.root / "fallback.pdf"
        fake.write_bytes(b"%PDF- fallback")
        with patch.object(pd, "_fallback", return_value=(fake, 1, ["full"])) as fallback:
            output, pages, warnings = self._compile()
        self.assertEqual((output, pages), (fake, 1))
        self.assertIn("pageIdInfo.dat", fallback.call_args.args[3])
        fallback.assert_called_once()

        _write_source(self.source, {a: b"A"})
        media = self.source / "media"
        media.mkdir()
        (media / "paper.pdf").write_bytes(b"%PDF- paper")
        with patch.object(pd, "_fallback", return_value=(fake, 1, ["full"])) as fallback:
            _output, _pages, warnings = self._compile()
        self.assertIn("embedded PDF", fallback.call_args.args[3])
        fallback.assert_called_once()

    def test_cache_budget_refusal_still_renders_without_writing(self) -> None:
        a = _uuid(41)
        _write_source(self.source, {a: b"A"})
        with patch.object(pd._WriteBudget, "allow", return_value=False):
            output, pages, warnings = self._compile()
        self.assertEqual(pages, 1)
        self.assertTrue(output.is_file())
        self.assertTrue(any("cache write skipped" in warning for warning in warnings))
        self.assertEqual(list(self.cache.rglob("entry.json")), [])


    def test_searchable_hook_runs_once_per_cache_miss(self) -> None:
        a = _uuid(51)
        _write_source(self.source, {a: b"A"})
        seen: list[str] = []

        def hook(page_pdf: Path, page_id: str, work: Path) -> Path:
            seen.append(page_id)
            target = work / f"searchable-{page_id}.pdf"
            target.write_bytes(page_pdf.read_bytes())
            return target

        pd.compile_note(self.source, self.root / "work", searchable_hook=hook)
        self.assertEqual(seen, [a])
        entry = self._entry_for(a)
        self.assertTrue((entry / "searchable.pdf").is_file())
        meta = json.loads((entry / "entry.json").read_text())
        self.assertIsNotNone(meta["searchable"])
        pd.compile_note(self.source, self.root / "work", searchable_hook=hook)
        self.assertEqual(seen, [a])


    def test_stale_note_manifest_is_replaced_atomically(self) -> None:
        a, b = _uuid(61), _uuid(62)
        _write_source(self.source, {a: b"A", b: b"B"})
        self._compile()
        manifests = list((self.cache / pd.CACHE_SCHEMA / "notes").glob("*.json"))
        self.assertEqual(len(manifests), 1)
        broken = json.loads(manifests[0].read_text())
        broken["count"] = 99
        manifests[0].write_text(json.dumps(broken))
        _output, pages, warnings = self._compile()
        self.assertEqual(pages, 2)
        self.assertTrue(any("stale cache manifest" in warning for warning in warnings))
        fresh = json.loads(manifests[0].read_text())
        self.assertEqual(fresh["count"], 2)
        self.assertEqual(len(fresh["pages"]), 2)


    def test_trailing_compat_page_is_dropped_by_visible_count(self) -> None:
        a, b, compat = _uuid(71), _uuid(72), _uuid(73)
        _write_source(self.source, {a: b"A", b: b"B", compat: b"compat"})

        def visible_probe(archive: Path):
            ids = [page_id for page_id, _hash in pd._read_page_manifest(archive)]
            info = [{"index": i, "template": "blank", "strokes": 1}
                    for i in range(len(ids) - 1)]
            return len(ids) - 1, info, []

        with patch.object(pd, "_probe_archive", visible_probe):
            output, pages, warnings = self._compile()
        self.assertEqual(pages, 2)
        self.assertEqual(self.calls, [a, b])
        self.assertFalse(any("fallback" in warning for warning in warnings))
        with pikepdf.open(output) as pdf:
            self.assertEqual(len(pdf.pages), 2)


    def test_flowing_body_text_is_detected_and_marked_layout_sensitive(self) -> None:
        a = _uuid(81)
        self.assertFalse(pd._note_body_has_text(_note_with_body("")))
        self.assertTrue(pd._note_body_has_text(_note_with_body("typed body")))
        _write_source(self.source, {a: b"A"})
        (self.source / "note.note").write_bytes(_note_with_body("typed body"))
        _output, pages, warnings = self._compile()
        self.assertEqual(pages, 1)
        self.assertTrue(any("flowing text" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
