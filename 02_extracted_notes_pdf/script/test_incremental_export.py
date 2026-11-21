"""Regression checks for changed-note selection and incomplete output rejection."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import incremental_export as ie


class IncrementalTests(unittest.TestCase):
    """Exercise recorded timestamps, safety and independent PDF validation."""

    def test_one_changed_note_plans_only_one_of_two_hundred(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            with patch.object(ie, "STATE", root / "missing-state.sqlite"):
                state = ie.open_state(readonly=True)
            live = {}
            for index in range(200):
                uuid = f"00000000-0000-0000-0000-{index:012x}"
                note = {
                    "uuid": uuid,
                    "title": f"note-{index}",
                    "folder": "maths",
                    "modified": 1,
                    "payload_bytes": 1,
                }
                live[uuid] = note
                state.execute(
                    """INSERT INTO exports(
                         uuid,modified,title,folder,path,sha256,renderer,pages,
                         original_sha256,derived_path,derived_sha256,derived_chars,
                         derived_renderer
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid, 1, note["title"], "maths", str(root / f"{uuid}.pdf"),
                     "source", ie.STRUCTURE_REVISION, 1, "source",
                     str(root / f"{uuid}.pdf"), "derived", 10,
                     ie.STRUCTURE_REVISION),
                )
            changed_uuid = next(iter(live))
            live[changed_uuid]["modified"] = 2
            try:
                with patch.object(
                    ie, "existing_output",
                    side_effect=lambda _row, uuid: root / f"{uuid}.pdf",
                ):
                    planned = ie._plan_batch(state, live, False, None, None)
                self.assertEqual([item["uuid"] for item in planned], [changed_uuid])
            finally:
                state.close()

    def test_verified_empty_rows_are_reported_only_while_signature_matches(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            with patch.object(ie, "STATE", Path(temporary) / "missing-state.sqlite"):
                state = ie.open_state(readonly=True)
            live = {
                "00000000-0000-0000-0000-000000000001": {
                    "uuid": "00000000-0000-0000-0000-000000000001",
                    "title": "Blank note", "folder": "maths", "modified": 7,
                },
                "00000000-0000-0000-0000-000000000002": {
                    "uuid": "00000000-0000-0000-0000-000000000002",
                    "title": "Changed blank", "folder": "maths", "modified": 8,
                },
            }
            state.execute(
                "INSERT INTO empty_sources VALUES(?,?,?,?,?,?,?)",
                (live[next(iter(live))]["uuid"], 7, "Blank note", "maths", 1,
                 "2026-11-21T00:00:00Z", "no ink records or embedded PDF"),
            )
            state.execute(
                "INSERT INTO empty_sources VALUES(?,?,?,?,?,?,?)",
                (live["00000000-0000-0000-0000-000000000002"]["uuid"], 1,
                 "Changed blank", "maths", 1, "2026-11-21T00:00:00Z", "stale"),
            )
            try:
                rows = ie._verified_empty_records(state, live)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "verified_empty")
                self.assertEqual(rows[0]["uuid"], "00000000-0000-0000-0000-000000000001")
            finally:
                state.close()

    def test_root_note_does_not_get_an_invented_folder(self):
        note = {
            "uuid": "00000000-0000-0000-0000-000000000003",
            "title": "Root note", "folder": "",
        }
        target = ie.target_for(note)
        self.assertEqual(target.parent, ie.DERIVED)
        self.assertIn(f"__{note['uuid']}.pdf", target.name)

    def test_seed_does_not_overwrite_exported_epoch(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            compressed = root / "compressed"
            compressed.mkdir()
            pdf = compressed / "old.pdf"
            pdf.write_bytes(b"%PDF- old")
            tracker = root / "tracker.sqlite"
            with sqlite3.connect(tracker) as c:
                c.execute("CREATE TABLE notes(uuid,last_modified,title,folder_path,host_file_path,sha256,export_status)")
                c.execute("INSERT INTO notes VALUES('u',100,'T','F',?,'h','SUCCESS')", (str(pdf),))
            with patch.multiple(ie, STATE=root / "state.sqlite", TRACKER=tracker, DERIVED=compressed):
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            work = Path(tmp)
            source = work / "source"
            source.mkdir()
            with patch.object(ie, "execute", return_value="4 visible page(s)\nIntegrity pages: 3 matched, 1 mismatched, 0 unavailable"):
                with self.assertRaisesRegex(ValueError, "inconsistent"):
                    ie.render(source, work)

    def test_pdf_validation_cache_reuses_only_unchanged_files(self):
        """Repeated checks decode once; replacing the file requires a new decode."""
        import pikepdf
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = self._pdf(Path(tmp) / "note.pdf", "original")
            ie._verified_pdf.cache_clear()
            with patch.object(pikepdf, "open", wraps=pikepdf.open) as parsed:
                self.assertEqual(ie.verify(path), 1)
                self.assertEqual(ie.verify(path, 1), 1)
                self.assertEqual(parsed.call_count, 1)
                with self.assertRaises(ValueError):
                    ie.verify(path, 2)
                replacement = self._pdf(Path(tmp) / "replacement.pdf", "changed")
                os.replace(replacement, path)
                self.assertEqual(ie.verify(path), 1)
                self.assertEqual(parsed.call_count, 2)

    def test_pdf_validation_cache_detects_equal_size_rewrite(self):
        """An in-place rewrite with restored mtime still invalidates the cache."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = self._pdf(Path(tmp) / "note.pdf", "original")
            self.assertEqual(ie.verify(path), 1)
            before = path.stat()
            payload = path.read_bytes()
            path.write_bytes(b"xxxxx" + payload[5:])
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(ValueError, "bad PDF header"):
                ie.verify(path)

    def test_blank_render_is_rejected_but_ink_passes(self):
        import pikepdf
        import fitz
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            blank = Path(tmp) / "blank.pdf"
            pdf = pikepdf.Pdf.new()
            pdf.add_blank_page()
            pdf.save(blank)
            with self.assertRaisesRegex(ValueError, "blank"):
                ie.content_pages(blank, 1)
            self.assertEqual(ie.verify(blank, 1), 1)  # structural check alone is insufficient

            vector = Path(tmp) / "thin-vector.pdf"
            with fitz.open() as document:
                document.new_page(width=400, height=500)
                page = document.new_page(width=400, height=500)
                page.draw_line((20, 250), (380, 250), color=(0.08, 0.12, 0.15), width=0.1)
                document.new_page(width=400, height=500)
                document.save(vector)
            self.assertGreater(ie.content_pages(vector, 3, [2]), 0)

    def _pdf(self, path: Path, text: str) -> Path:
        import fitz
        path.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open() as pdf:
            pdf.new_page().insert_text((40, 70), text)
            pdf.save(path)
        return path

    def test_output_valid_rejects_missing_and_broken_symlink(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            real = self._pdf(root / "real.pdf", "real note")
            self.assertTrue(ie.output_valid(real))
            self.assertFalse(ie.output_valid(root / "absent.pdf"))
            broken = root / "broken.pdf"
            broken.symlink_to(root / "gone.pdf")
            self.assertFalse(ie.output_valid(broken))

    def test_existing_output_never_follows_a_symlink(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            compressed = Path(tmp) / "compressed"
            compressed.mkdir()
            uuid = "c8860b15-10b6-5391-0000-0196be0869de"
            link = compressed / f"F/note__{uuid}.pdf"
            link.parent.mkdir()
            link.symlink_to(Path(tmp) / "gone.pdf")
            with patch.object(ie, "DERIVED", compressed):
                self.assertIsNone(ie.existing_output({"path": str(link)}, uuid))

    def test_known_uuid_path_only_prunes_owned_paths(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            uuid = "c8860b15-10b6-5391-0000-0196be0869de"
            compressed = root / "compressed"
            owned = self._pdf(compressed / f"F/note__{uuid}.pdf", "owned")
            foreign = self._pdf(root / f"outside/note__{uuid}.pdf", "foreign")
            keep = compressed / f"F/keep__{uuid}.pdf"
            old_bytes = owned.read_bytes()
            with patch.object(ie, "DERIVED", compressed), \
                 patch.object(ie, "HIST", root / "history"):
                self.assertTrue(ie.known_uuid_path(owned, uuid))
                self.assertFalse(ie.known_uuid_path(foreign, uuid))
                self.assertTrue(ie.prune_old(owned, uuid, keep))
                self.assertFalse(owned.exists())
                archived = list((root / "history/preserved_originals/superseded_exports").rglob("*.pdf"))
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].read_bytes(), old_bytes)
                self.assertFalse(ie.prune_old(foreign, uuid, keep))
                self.assertTrue(foreign.exists())

    def test_stale_structure_marker_forces_one_time_reexport(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            compressed = Path(tmp) / "compressed"
            uuid = "c8860b15-10b6-5391-0000-0196be0869de"
            final = self._pdf(compressed / f"F/note__{uuid}.pdf", "existing bytes")
            note = {"uuid": uuid, "modified": 7, "title": "note", "folder": "F"}
            row = {"modified": 7, "title": "note", "folder": "F", "path": str(final),
                   "derived_path": None, "renderer": ie.STRUCTURE_REVISION,
                   "derived_renderer": ie.STRUCTURE_REVISION}
            with patch.object(ie, "DERIVED", compressed):
                self.assertFalse(ie.needs_export(row, note))
                # Same bytes and mtime, but an older derivation may have lost links.
                row["derived_renderer"] = None
                self.assertTrue(ie.needs_export(row, note))
                row["derived_renderer"] = ie.STRUCTURE_REVISION
                final.unlink()
                self.assertTrue(ie.needs_export(row, note))

    def test_derive_target_is_standalone_after_source_removal(self):
        import derive_corpus as dc
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source = self._pdf(root / "stage/note.pdf", "searchable body text")
            target = root / "compressed/F/note__uuid.pdf"
            with patch.object(dc, "COMPRESSED", root / "compressed"), \
                 patch.object(dc, "guard"):
                result = dc.derive_file(source, target=target, ocr=False)
            self.assertTrue(target.is_file())
            self.assertFalse(target.is_symlink())
            self.assertEqual(result["storage"], "standalone")
            for key in ("sha256", "pages", "chars", "source_sha256"):
                self.assertIn(key, result)
            source.unlink()
            self.assertTrue(target.is_file())

    def test_existing_output_rejects_same_pdf_with_wrong_recorded_digest(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            compressed = Path(tmp) / "compressed"
            uuid = "c8860b15-10b6-5391-0000-0196be0869de"
            final = self._pdf(compressed / f"F/note__{uuid}.pdf", "tampered bytes")
            row = {"path": str(final), "derived_path": None, "derived_sha256": "0" * 64}
            with patch.object(ie, "DERIVED", compressed):
                self.assertIsNone(ie.existing_output(row, uuid))
                row["derived_sha256"] = ie.digest(final)
                self.assertEqual(ie.existing_output(row, uuid), final)

    def test_prune_uuid_aliases_keeps_unsuffixed_and_other_uuids(self):
        uuid = "c8860b15-10b6-5391-0000-0196be0869de"
        other = "c8860b15-10b6-5391-0000-0196be0869df"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            compressed = Path(tmp) / "compressed"
            target = self._pdf(compressed / f"F/note__{uuid}.pdf", "canonical")
            alias = self._pdf(compressed / f"Old/note__{uuid}.pdf", "old alias")
            unsuffixed = self._pdf(compressed / "F/plain.pdf", "plain")
            foreign = self._pdf(compressed / f"F/other__{other}.pdf", "other")
            alias_bytes = alias.read_bytes()
            with patch.object(ie, "DERIVED", compressed), \
                 patch.object(ie, "HIST", Path(tmp) / "history"):
                removed = ie.prune_uuid_aliases(uuid, target)
            self.assertTrue(removed)
            self.assertTrue(target.is_file())
            self.assertFalse(alias.exists())
            self.assertTrue(Path(removed[0]).is_file())
            self.assertEqual(Path(removed[0]).read_bytes(), alias_bytes)
            self.assertTrue(unsuffixed.is_file())
            self.assertTrue(foreign.is_file())

    def test_postprocess_never_derives_and_reports_pending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            state = root / "state.sqlite"
            with sqlite3.connect(state) as connection:
                connection.execute("CREATE TABLE exports(uuid TEXT PRIMARY KEY, modified INTEGER, "
                                   "title TEXT, folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, "
                                   "pages INTEGER)")
                connection.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)",
                                   ("u", 1, "t", "F", str(root / "external.pdf"), "x", "x", 1))
            with patch.multiple(ie, STATE=state, TRACKER=root / "none.sqlite",
                                HIST=root, DERIVED=root / "compressed", ROOT=root), \
                 patch.object(ie, "refresh_mirror"), \
                 patch("derive_corpus.derive_file", side_effect=AssertionError("must not derive")):
                self.assertEqual(ie.postprocess(), 2)

    def test_two_notes_never_share_cache_or_leave_scratch(self):
        first = "c8860b15-10b6-5391-0000-0196be0869de"
        second = "c8860b15-10b6-5391-0000-0196be0869df"
        live = {first: {"uuid": first, "title": "one", "folder": "F", "modified": 1,
                        "payload_bytes": 1000},
                second: {"uuid": second, "title": "two", "folder": "F", "modified": 1,
                         "payload_bytes": 1000}}
        compile_calls, derive_calls = [], []

        def fake_compile(source, work, *, searchable_hook=None):
            compile_calls.append((Path(source), Path(work)))
            work.mkdir(parents=True, exist_ok=True)
            raw = self._pdf(work / "page-delta-complete.pdf", "raw note")
            if len(compile_calls) == 1:
                self._pdf(work / "page-delta-searchable.pdf", "searchable note")
            return raw, 1, ["ok"]

        def fake_derive(source, *, target=None, ocr=False):
            source, target = Path(source), Path(target)
            derive_calls.append({"source": source,
                                 "leaked": (source.parent / "page-delta-searchable.pdf").exists(),
                                 "ocr": ocr})
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sha = ie.digest(target)
            return {"sha256": sha, "source_sha256": sha, "pages": 1, "chars": 10,
                    "status": "searchable", "ocr": True, "sparse_pages": 0}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self._pdf(root / "compressed" / "F" / f"one__{first}.pdf", "old output")
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                 DERIVED=root / "compressed", PROJECT_ROOT=root,
                                 inventory=lambda work, uuid=None, **kwargs: (
                                    live if uuid is None else
                                    ({uuid: live[uuid]} if uuid in live else {}))), \
                 patch.object(ie, "execute"), patch.object(ie, "transport", return_value=[]), \
                 patch("derive_corpus.guard"), \
                 patch("derive_corpus.page_ocr_hook", lambda *a, **k: None), \
                 patch("derive_corpus.verify_pair", lambda *a, **k: (1, 0)), \
                 patch("derive_corpus.derive_file", side_effect=fake_derive), \
                 patch("page_delta.compile_note", side_effect=fake_compile):
                self.assertEqual(ie.run(None, False), 0)
            self.assertEqual(len(compile_calls), 2)
            self.assertEqual(compile_calls[0][0].name, second)  # missing output first
            self.assertNotEqual(compile_calls[0][1], compile_calls[1][1])
            self.assertFalse(compile_calls[0][1].exists())
            self.assertFalse(compile_calls[1][1].exists())
            self.assertFalse(compile_calls[0][1].parent.exists())  # whole scratch gone
            self.assertFalse((root / "stage" / first).exists())
            self.assertFalse((root / "stage" / second).exists())
            self.assertFalse(derive_calls[0]["ocr"])
            self.assertTrue(derive_calls[0]["leaked"])
            self.assertTrue(derive_calls[1]["ocr"])
            self.assertFalse(derive_calls[1]["leaked"])
            snapshot = json.loads((root / "hist/live_inventory.private.json").read_text())
            self.assertEqual(snapshot["active"], 2)
            self.assertEqual({note["uuid"] for note in snapshot["notes"]}, {first, second})

    def test_bad_searchable_cache_falls_back_to_fresh_compiled_output(self):
        """A cache rendering mismatch must not block a safe fresh derivation."""
        uuid = "c8860b15-10b6-5391-0000-0196be0869de"
        note = {"uuid": uuid, "title": "note", "folder": "F", "modified": 1,
                "payload_bytes": 1000}
        derive_calls = []

        def fake_inventory(work, only=None, **kwargs):
            return {uuid: dict(note)}

        def fake_compile(source, work, *, searchable_hook=None):
            work.mkdir(parents=True, exist_ok=True)
            output = self._pdf(work / "page-delta-complete.pdf", "authoritative render")
            self._pdf(work / "page-delta-searchable.pdf", "bad cached render")
            return output, 1, ["compile warning"]

        def fake_verify_pair(original, candidate):
            if Path(candidate).name == "page-delta-searchable.pdf":
                raise ValueError("Derived PDF changed visible content")
            return 1, 10

        def fake_derive(source, *, target=None, ocr=False):
            source, target = Path(source), Path(target)
            derive_calls.append((source.name, ocr))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sha = ie.digest(target)
            return {"sha256": sha, "source_sha256": sha, "pages": 1, "chars": 10,
                    "status": "searchable", "ocr": ocr, "sparse_pages": 1}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                DERIVED=root / "compressed", PROJECT_ROOT=root), \
                 patch.object(ie, "inventory", side_effect=fake_inventory), \
                 patch.object(ie, "execute"), patch.object(ie, "transport", return_value=[]), \
                 patch("derive_corpus.guard"), \
                 patch("derive_corpus.page_ocr_hook", lambda *a, **k: None), \
                 patch("derive_corpus.verify_pair", side_effect=fake_verify_pair), \
                 patch("derive_corpus.derive_file", side_effect=fake_derive), \
                 patch("page_delta.compile_note", side_effect=fake_compile):
                self.assertEqual(ie.run(None, False), 0)
            report = json.loads((root / "hist/incremental_report.json").read_text())
        self.assertEqual(derive_calls, [("page-delta-complete.pdf", True)])
        self.assertFalse(report["updated"][0]["derived_from_page_cache"])
        self.assertTrue(any("rejected cached searchable output" in warning
                            for warning in report["updated"][0]["warnings"]))

    def test_inventory_filters_one_uuid_and_rejects_bad_filter(self):
        uuid = "c8860b15-10b6-5391-0000-0196be0869de"
        payload = json.dumps([{"uuid": uuid, "title": "t", "folder": "F",
                               "modified": 1, "payload_bytes": 2}])
        calls, uploads = [], []

        def fake_execute(argv, timeout=180):
            calls.append(argv)
            return payload

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            work = Path(tmp)
            with patch.object(ie, "upload", side_effect=lambda *args: uploads.append(args)), \
                 patch.object(ie, "transport", return_value=["ssh"]), \
                 patch.object(ie, "execute", side_effect=fake_execute):
                self.assertEqual(ie.inventory(work, uuid), {uuid: json.loads(payload)[0]})
                self.assertTrue(calls[0][-1].endswith(" " + uuid))
                self.assertEqual(ie.inventory(work, uuid, upload_helper=False),
                                 {uuid: json.loads(payload)[0]})
                helper = (work / "inventory.py").read_text()
                self.assertIn("AND UUID=?", helper)  # filter is bound, not formatted
                with self.assertRaises(ValueError):
                    ie.inventory(work, "../../etc/wdoc")
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(uploads), 1)

    def test_postprocess_audits_compressed_rows_without_deriving(self):
        import hashlib
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            compressed = root / "compressed"
            uuid = "c8860b15-10b6-5391-0000-0196be0869de"
            final = self._pdf(compressed / f"F/note__{uuid}.pdf", "final text")
            state = root / "state.sqlite"
            with sqlite3.connect(state) as connection:
                connection.execute("CREATE TABLE exports(uuid TEXT PRIMARY KEY, modified INTEGER, "
                                   "title TEXT, folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, "
                                   "pages INTEGER)")
                connection.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)",
                                   (uuid, 1, "note", "F", str(final), "old", "x", 1))
            with patch.multiple(ie, STATE=state, TRACKER=root / "none.sqlite",
                                HIST=root, DERIVED=compressed, ROOT=root,
                                PROJECT_ROOT=root), \
                 patch.object(ie, "refresh_mirror"), \
                 patch("derive_corpus.derive_file", side_effect=AssertionError("must not derive")):
                self.assertEqual(ie.postprocess(), 0)
                with sqlite3.connect(state) as connection:
                    row = connection.execute(
                        "SELECT derived_path, derived_sha256 FROM exports WHERE uuid=?",
                        (uuid,)).fetchone()
            self.assertEqual(row[0], str(final))
            self.assertEqual(row[1], hashlib.sha256(final.read_bytes()).hexdigest())

    def test_repair_repoints_and_prunes_only_validated_duplicates(self):
        import hashlib
        uuid_a = "c8860b15-10b6-5391-0000-0196be0869de"
        uuid_b = "c8860b15-10b6-5391-0000-0196be0869df"
        uuid_c = "c8860b15-10b6-5391-0000-0196be0869d0"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            compressed = root / "compressed"
            legacy = self._pdf(compressed / "F/legacy_a.pdf", "legacy note body")
            canonical_b = self._pdf(compressed / f"F/dupB__{uuid_b}.pdf", "duplicate body")
            old_b = compressed / f"Old/dupB__{uuid_b}.pdf"
            old_b.parent.mkdir(parents=True)
            shutil.copyfile(canonical_b, old_b)  # byte-identical duplicate
            canonical_c = self._pdf(compressed / f"F/dupC__{uuid_c}.pdf", "one text")
            old_c = self._pdf(compressed / f"Old/dupC__{uuid_c}.pdf", "different text")
            state = root / "state.sqlite"
            with sqlite3.connect(state) as connection:
                connection.execute(
                    "CREATE TABLE exports(uuid TEXT PRIMARY KEY, modified INTEGER, title TEXT, "
                    "folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, pages INTEGER)")
                for uuid, title, path, derived in (
                        (uuid_a, "legacy_a", str(root / "gone.pdf"), str(legacy)),
                        (uuid_b, "dupB", str(canonical_b), str(canonical_b)),
                        (uuid_c, "dupC", str(canonical_c), str(canonical_c))):
                    connection.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)",
                                       (uuid, 1, title, "F", path, "x", "x", 1))
                connection.execute("ALTER TABLE exports ADD COLUMN derived_path TEXT")
                connection.execute("ALTER TABLE exports ADD COLUMN derived_sha256 TEXT")
                connection.execute("ALTER TABLE exports ADD COLUMN derived_chars INTEGER")
                connection.execute("ALTER TABLE exports ADD COLUMN original_sha256 TEXT")
                connection.execute("UPDATE exports SET derived_path=? WHERE uuid=?", (str(legacy), uuid_a))
                connection.execute("UPDATE exports SET derived_path=? WHERE uuid=?", (str(canonical_b), uuid_b))
                connection.execute("UPDATE exports SET derived_path=? WHERE uuid=?", (str(canonical_c), uuid_c))
            with patch.multiple(ie, STATE=state, TRACKER=root / "none.sqlite",
                                HIST=root, DERIVED=compressed, ROOT=root,
                                PROJECT_ROOT=root), \
                 patch.object(ie, "refresh_mirror"):
                self.assertEqual(ie.repair(apply=True), 2)  # explicit conflict
                with sqlite3.connect(state) as connection:
                    paths = dict(connection.execute("SELECT uuid, path FROM exports"))
                    revisions = dict(connection.execute(
                        "SELECT uuid, derived_renderer FROM exports"))
            # Repair must not claim the new structure revision for old bytes.
            self.assertIsNone(revisions[uuid_a])
            canonical_a = compressed / f"F/legacy_a__{uuid_a}.pdf"
            self.assertEqual(paths[uuid_a], str(canonical_a))
            self.assertTrue(canonical_a.is_file())
            self.assertFalse(legacy.exists())
            self.assertFalse(old_b.exists())
            # A differing copy is reported as a conflict and kept, never merged.
            self.assertTrue(old_c.exists())
            self.assertTrue(canonical_c.is_file())
            self.assertNotEqual(hashlib.sha256(old_c.read_bytes()).hexdigest(),
                                hashlib.sha256(canonical_c.read_bytes()).hexdigest())

    def test_only_source_confirmed_empty_notes_are_cached_as_skips(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
            media = source / "media"
            media.mkdir()
            (media / "note-image.png").write_bytes(b"nonempty image payload")
            with patch.object(ie, "probe_archive", return_value=(1, [{"strokes": 0}], [])), \
                 patch("page_delta._note_layout_sensitive", return_value=False), \
                 patch.object(ie, "embedded_pdfs", return_value=[]):
                self.assertEqual(ie.source_is_empty(source, archive), (False, 1, 0))


    def test_notes_synced_midrun_are_exported_from_authoritative_inventory(self):
        """A snapshot taken at start must not omit notes arriving mid-export."""
        first = "c8860b15-10b6-5391-0000-0190dd9b0be5"
        second = "c8860b15-10b6-5391-0000-0190dd9b0be6"
        third = "c8860b15-10b6-5391-0000-0190dd9b0be7"
        all_notes = {
            first: {"uuid": first, "title": "one", "folder": "F", "modified": 1,
                    "payload_bytes": 1000},
            second: {"uuid": second, "title": "two", "folder": "F", "modified": 2,
                     "payload_bytes": 1000},
            third: {"uuid": third, "title": "three", "folder": "F", "modified": 3,
                    "payload_bytes": 1000},
        }
        # The authoritative VM inventory grows after the initial snapshot.
        snapshots = [
            {first: all_notes[first], second: all_notes[second]},
            dict(all_notes),
            dict(all_notes),
        ]
        full_calls, compiled = [], []

        def fake_inventory(work, uuid=None, **kwargs):
            if uuid is not None:
                return {uuid: all_notes[uuid]} if uuid in all_notes else {}
            index = min(len(full_calls), len(snapshots) - 1)
            full_calls.append(index)
            return dict(snapshots[index])

        def fake_compile(source, work, *, searchable_hook=None):
            work.mkdir(parents=True, exist_ok=True)
            compiled.append(Path(work).name)
            return self._pdf(work / "page-delta-complete.pdf", "raw note"), 1, ["ok"]

        def fake_derive(source, *, target=None, ocr=False):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sha = ie.digest(target)
            return {"sha256": sha, "source_sha256": sha, "pages": 1, "chars": 10,
                    "status": "searchable", "ocr": True, "sparse_pages": 0}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                DERIVED=root / "compressed", PROJECT_ROOT=root), \
                 patch.object(ie, "inventory", side_effect=fake_inventory), \
                 patch.object(ie, "execute"), patch.object(ie, "transport", return_value=[]), \
                 patch("derive_corpus.guard"), \
                 patch("derive_corpus.page_ocr_hook", lambda *a, **k: None), \
                 patch("derive_corpus.verify_pair", lambda *a, **k: (1, 0)), \
                 patch("derive_corpus.derive_file", side_effect=fake_derive), \
                 patch("page_delta.compile_note", side_effect=fake_compile):
                self.assertEqual(ie.run(None, False), 0)
            report = json.loads((root / "hist/incremental_report.json").read_text())
            snapshot = json.loads((root / "hist/live_inventory.private.json").read_text())
        self.assertEqual(set(compiled), {first, second, third})
        self.assertEqual(compiled[-1], third)  # note synced mid-run is last
        self.assertEqual(report["active"], 3)  # final authoritative snapshot
        self.assertEqual(report["initial_active"], 2)
        self.assertEqual(report["authoritative_active"], 3)
        self.assertEqual(report["sync_passes"], 1)
        self.assertEqual(report["pending_sync"], [])
        self.assertEqual(report["planned"], 3)
        self.assertEqual(snapshot["active"], 3)

    def test_unseen_authoritative_notes_are_reported_pending_sync(self):
        """Notes that appear past the bounded recheck limit are not hidden."""
        first = "c8860b15-10b6-5391-0000-0190dd9b0be5"
        second = "c8860b15-10b6-5391-0000-0190dd9b0be6"
        notes = {
            first: {"uuid": first, "title": "one", "folder": "F", "modified": 1,
                    "payload_bytes": 1000},
            second: {"uuid": second, "title": "two", "folder": "F", "modified": 2,
                     "payload_bytes": 1000},
        }
        full_calls = []

        def fake_inventory(work, uuid=None, **kwargs):
            if uuid is not None:
                return {uuid: notes[uuid]} if uuid in notes else {}
            full_calls.append(1)
            return dict(notes) if len(full_calls) > 1 else {first: notes[first]}

        def fake_compile(source, work, *, searchable_hook=None):
            work.mkdir(parents=True, exist_ok=True)
            return self._pdf(work / "page-delta-complete.pdf", "raw note"), 1, ["ok"]

        def fake_derive(source, *, target=None, ocr=False):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sha = ie.digest(target)
            return {"sha256": sha, "source_sha256": sha, "pages": 1, "chars": 10,
                    "status": "searchable", "ocr": True, "sparse_pages": 0}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                DERIVED=root / "compressed", PROJECT_ROOT=root,
                                SYNC_RECHECK_PASSES=1), \
                 patch.object(ie, "inventory", side_effect=fake_inventory), \
                 patch.object(ie, "execute"), patch.object(ie, "transport", return_value=[]), \
                 patch("derive_corpus.guard"), \
                 patch("derive_corpus.page_ocr_hook", lambda *a, **k: None), \
                 patch("derive_corpus.verify_pair", lambda *a, **k: (1, 0)), \
                 patch("derive_corpus.derive_file", side_effect=fake_derive), \
                 patch("page_delta.compile_note", side_effect=fake_compile):
                self.assertEqual(ie.run(None, False), 2)
            report = json.loads((root / "hist/incremental_report.json").read_text())
        self.assertEqual([item["uuid"] for item in report["updated"]], [first])
        self.assertEqual(report["pending_sync"], [second])
        self.assertEqual(report["authoritative_active"], 2)


    def test_changed_signature_is_reprocessed_not_marked_complete(self):
        """UUID reuse is not enough: an edited note must be exported again."""
        uuid = "c8860b15-10b6-5391-0000-0190dd9b0be8"
        original = {"uuid": uuid, "title": "one", "folder": "F", "modified": 1,
                    "payload_bytes": 1000}
        edited = {**original, "modified": 2, "title": "one-renamed"}
        current = {uuid: dict(original)}
        full_calls, compiled = [], []

        def fake_inventory(work, uuid_filter=None, **kwargs):
            if uuid_filter is not None:
                record = current.get(uuid_filter)
                return {uuid_filter: dict(record)} if record else {}
            full_calls.append(1)
            if len(full_calls) == 2:  # guest edits the note mid-run
                current[uuid] = dict(edited)
            return {u: dict(note) for u, note in current.items()}

        def fake_compile(source, work, *, searchable_hook=None):
            work.mkdir(parents=True, exist_ok=True)
            compiled.append(Path(work).name)
            return self._pdf(work / "page-delta-complete.pdf", "raw note"), 1, ["ok"]

        def fake_derive(source, *, target=None, ocr=False):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sha = ie.digest(target)
            return {"sha256": sha, "source_sha256": sha, "pages": 1, "chars": 10,
                    "status": "searchable", "ocr": True, "sparse_pages": 0}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                DERIVED=root / "compressed", PROJECT_ROOT=root), \
                 patch.object(ie, "inventory", side_effect=fake_inventory), \
                 patch.object(ie, "execute"), patch.object(ie, "transport", return_value=[]), \
                 patch("derive_corpus.guard"), \
                 patch("derive_corpus.page_ocr_hook", lambda *a, **k: None), \
                 patch("derive_corpus.verify_pair", lambda *a, **k: (1, 0)), \
                 patch("derive_corpus.derive_file", side_effect=fake_derive), \
                 patch("page_delta.compile_note", side_effect=fake_compile):
                self.assertEqual(ie.run(None, False), 0)
            report = json.loads((root / "hist/incremental_report.json").read_text())
        self.assertEqual(compiled, [uuid, uuid])
        self.assertEqual(len(report["updated"]), 2)
        self.assertEqual(report["sync_passes"], 1)
        self.assertEqual(report["pending_sync"], [])
        self.assertEqual(report["active"], 1)

    def test_inventory_recheck_failure_is_reported_and_nonzero(self):
        """A failed authoritative recheck must never be reported as success."""
        uuid = "c8860b15-10b6-5391-0000-0190dd9b0be9"
        note = {"uuid": uuid, "title": "one", "folder": "F", "modified": 1,
                "payload_bytes": 1000}
        full_calls = []

        def fake_inventory(work, uuid_filter=None, **kwargs):
            if uuid_filter is not None:
                return {uuid_filter: dict(note)} if uuid_filter == uuid else {}
            full_calls.append(1)
            if len(full_calls) > 1:
                raise RuntimeError("VM unreachable")
            return {uuid: dict(note)}

        def fake_compile(source, work, *, searchable_hook=None):
            work.mkdir(parents=True, exist_ok=True)
            return self._pdf(work / "page-delta-complete.pdf", "raw note"), 1, ["ok"]

        def fake_derive(source, *, target=None, ocr=False):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            sha = ie.digest(target)
            return {"sha256": sha, "source_sha256": sha, "pages": 1, "chars": 10,
                    "status": "searchable", "ocr": True, "sparse_pages": 0}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                DERIVED=root / "compressed", PROJECT_ROOT=root), \
                 patch.object(ie, "inventory", side_effect=fake_inventory), \
                 patch.object(ie, "execute"), patch.object(ie, "transport", return_value=[]), \
                 patch("derive_corpus.guard"), \
                 patch("derive_corpus.page_ocr_hook", lambda *a, **k: None), \
                 patch("derive_corpus.verify_pair", lambda *a, **k: (1, 0)), \
                 patch("derive_corpus.derive_file", side_effect=fake_derive), \
                 patch("page_delta.compile_note", side_effect=fake_compile):
                self.assertEqual(ie.run(None, False), 2)
            report = json.loads((root / "hist/incremental_report.json").read_text())
        self.assertTrue(report["inventory_recheck_failed"])
        self.assertIn("VM unreachable", report["inventory_recheck_error"])
        self.assertEqual(report["active"], 1)
        self.assertEqual(report["pending_sync"], [])
        self.assertEqual(len(report["updated"]), 1)

    def test_target_for_rejects_invalid_uuid(self):
        """A malformed UUID can never become a filesystem path component."""
        with patch.object(ie, "DERIVED", Path("/tmp/opencode/derived")):
            for bad in ("../../evil", "a/b", "not-a-uuid", ""):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    ie.target_for({"uuid": bad, "title": "note", "folder": "F"})

    def test_target_for_accepts_leaf_symlink_for_safe_replacement(self):
        """A symlink file may be replaced; its parent must still stay contained."""
        note = {"uuid": "c8860b15-10b6-5391-0000-019e1a62cef0",
                "title": "note", "folder": "F"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            derived = root / "compressed"
            (derived / "F").mkdir(parents=True)
            outside = root / "old-original.pdf"
            outside.write_bytes(b"old")
            with patch.object(ie, "DERIVED", derived), patch.object(ie, "PROJECT_ROOT", root):
                target = ie.target_for(note)
                target.symlink_to(outside)
                self.assertEqual(ie.target_for(note), target)
                self.assertTrue(target.is_symlink())

    def test_target_for_rejects_parent_symlink_escape(self):
        """A linked folder escaping the compressed root is never a target."""
        note = {"uuid": "c8860b15-10b6-5391-0000-019e1a62cef0",
                "title": "note", "folder": "F"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            derived = root / "compressed"
            derived.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (derived / "F").symlink_to(outside, target_is_directory=True)
            with patch.object(ie, "DERIVED", derived), patch.object(ie, "PROJECT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    ie.target_for(note)

    def test_invalid_uuid_is_planned_without_any_filesystem_probe(self):
        """A stored row must not make planning probe an escaped UUID path."""
        bad = "../../../../tmp/opencode/escaped"
        note = {"uuid": bad, "title": "note", "folder": "F", "modified": 1,
                "payload_bytes": 100}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            state = root / "state.sqlite"
            with sqlite3.connect(state) as connection:
                connection.execute(
                    "CREATE TABLE exports(uuid TEXT PRIMARY KEY, modified INTEGER, "
                    "title TEXT, folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, "
                    "pages INTEGER)")
                connection.execute(
                    "CREATE TABLE empty_sources(uuid TEXT PRIMARY KEY, modified INTEGER, "
                    "title TEXT, folder TEXT, visible_pages INTEGER, checked_at TEXT, "
                    "reason TEXT)")
                # A matching row would normally send needs_export() into
                # existing_output() and target_for() path lookups.
                connection.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)",
                                   (bad, 1, "note", "F", str(root / "old.pdf"),
                                    "x", "x", 1))
            with patch.object(ie, "DERIVED", root / "compressed"), \
                 patch.object(ie, "target_for",
                              side_effect=AssertionError("path lookup")), \
                 patch.object(ie, "existing_output",
                              side_effect=AssertionError("output probe")):
                with sqlite3.connect(state) as connection:
                    planned = ie._plan_batch(connection, {bad: note}, False, None, None)
            self.assertEqual(planned, [note])

    def test_invalid_inventory_uuid_fails_without_escaped_path_access(self):
        """End to end: a hostile UUID is an explicit failure, never a path."""
        bad = "../../../../tmp/opencode/escaped"
        note = {"uuid": bad, "title": "note", "folder": "F", "modified": 1,
                "payload_bytes": 100}
        real_target_for, real_rglob = ie.target_for, Path.rglob

        def guarded_target_for(value):
            if value["uuid"] == bad:
                raise AssertionError("target_for called with an invalid UUID")
            return real_target_for(value)

        def guarded_rglob(self, pattern):
            if bad in str(pattern):
                raise AssertionError(f"UUID-derived glob probe: {pattern}")
            return real_rglob(self, pattern)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            sentinel = root / "escaped.pdf"
            sentinel.write_bytes(b"sentinel")
            with patch.dict(os.environ, {"SN_EXPORT_WORK": str(root / "temp/export")}), \
                 patch.multiple(ie, HIST=root / "hist", STATE=root / "state.sqlite",
                                TRACKER=root / "none.sqlite", DEST=root / "stage",
                                DERIVED=root / "compressed", PROJECT_ROOT=root), \
                                 patch.object(ie, "inventory", side_effect=lambda work, uuid=None, **kwargs: (
                     {bad: dict(note)} if uuid is None or uuid == bad else {})), \
                 patch.object(ie, "target_for", side_effect=guarded_target_for), \
                 patch.object(Path, "rglob", guarded_rglob):
                rc = ie.run(None, False)
                report = json.loads((root / "hist/incremental_report.json").read_text())
                sentinel_intact = sentinel.read_bytes() == b"sentinel"
                compressed_created = (root / "compressed").exists()
        self.assertEqual(rc, 2)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("invalid UUID", report["failed"][0]["reason"])
        self.assertTrue(sentinel_intact)
        self.assertFalse(compressed_created)


if __name__ == "__main__":
    unittest.main()
