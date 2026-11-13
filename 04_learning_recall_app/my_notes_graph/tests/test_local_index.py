"""Offline, incremental, page-aware local corpus index tests.

These build a miniature export tree (valid PDFs behind deliberately broken
``compressed/`` placeholders, one blank PDF, one missing target, duplicate
basenames and a non-PDF) and exercise the real PyMuPDF/sklearn path with all
network syscalls disabled.
"""

from __future__ import annotations

import ast
import hashlib
import math
import os
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
sys.path.insert(0, str(APP_ROOT))
for site in (
    PROJECT_ROOT / "05_environments_and_venvs/python_ocr_env/site-packages",
    PROJECT_ROOT.parent / "python_libs/site-packages",
):
    if site.is_dir():
        sys.path.append(str(site))

from src import local_index  # noqa: E402

# Corpus tests exercise the deterministic offline path; the semantic local
# model is validated separately and is never required for the suite to pass.
os.environ["LOCAL_INDEX_EMBED_MODE"] = "lexical"
local_index.reset_embedder()


def _make_pdf(path: Path, pages: list[str]) -> None:
    """Create a local test PDF with one text block per page (no network)."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_textbox(fitz.Rect(72, 72, 540, 720), text, fontsize=11)
    document.save(str(path))
    document.close()


def _placeholder(compressed: Path, relative: str) -> None:
    """Create the exact broken relative link shape produced by the export split."""
    link = compressed / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path("../original") / relative)


def _digest(path: Path) -> str:
    """Hash a file to prove the indexer never rewrites source PDFs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalIndexTests(unittest.TestCase):
    """Verify status accounting, incremental replacement, citations and scoping."""

    def setUp(self) -> None:
        local_index.reset_embedder()
        self._temp = tempfile.TemporaryDirectory(prefix="local_index_test_")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name) / "notes"
        self.original = self.root / "original"
        self.compressed = self.root / "compressed"
        self.db = Path(self._temp.name) / "local_index.sqlite"

        _make_pdf(self.original / "maths/algebra/basics.pdf", [
            "Gradient descent updates weights with alpha = 0.01 and x_i.",
            "The chain rule gives d/dx f(g(x)) = f'(g(x)) g'(x) for derivatives.",
        ])
        _make_pdf(self.original / "maths/dup.pdf", [
            "Singular value decomposition factors a matrix into U S V transpose.",
        ])
        _make_pdf(self.original / "Thoughts/dup.pdf", [
            "Habit loops require a cue routine and reward to become automatic.",
        ])
        _make_pdf(self.original / "Mlaidlgenai_robo/Robotics/robot.pdf", [
            "Denavit Hartenberg parameters describe robot arm joint transforms.",
        ])
        _make_pdf(self.original / "maths/blank.pdf", [""])
        (self.original / "maths/notes.txt").write_text("not a pdf", encoding="utf-8")

        for relative in (
            "maths/algebra/basics.pdf",
            "maths/dup.pdf",
            "Thoughts/dup.pdf",
            "Mlaidlgenai_robo/Robotics/robot.pdf",
            "maths/blank.pdf",
            "maths/gone.pdf",
        ):
            _placeholder(self.compressed, relative)
        (self.compressed / "maths/notes.txt").write_text("ignored", encoding="utf-8")

        self.summary = local_index.index_all(self.root, db_path=self.db)

    # ------------------------------------------------------------------ #
    # Personality / offline guarantees
    # ------------------------------------------------------------------ #
    def test_embedding_mode_is_explicit_and_lexical_by_default(self):
        info = local_index.embedding_info()
        self.assertEqual(info["mode"], "lexical-hashing")
        self.assertFalse(info["semantic"])
        self.assertTrue(info["lexical_not_semantic"])
        self.assertFalse(info["external_api"])
        self.assertEqual(info["network"], "disabled")
        self.assertIn("NOT semantic", info["detail"])

    def test_module_has_no_remote_embedding_client(self):
        tree = ast.parse((APP_ROOT / "src/local_index.py").read_text(encoding="utf-8"))
        banned = {"gemini", "requests", "urllib", "urllib3", "httpx", "http", "socket", "aiohttp"}
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, sorted(imported & banned))
        self.assertTrue(str(local_index.DB_FILE).endswith("local_index.sqlite"))
        self.assertNotIn("recall.sqlite", str(local_index.DB_FILE))

    def _fake_model(self, directory: Path, complete: bool) -> Path:
        """Create a local model layout that validates without loading torch."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "tokenizer_config.json").write_text("{}", encoding="utf-8")
        if complete:
            (directory / "modules.json").write_text("[]", encoding="utf-8")
            (directory / "1_Pooling").mkdir(exist_ok=True)
            (directory / "1_Pooling/config.json").write_text("{}", encoding="utf-8")
            (directory / "model.safetensors").write_bytes(b"0" * (2 * 1024 * 1024))
        else:
            (directory / "model.safetensors").write_bytes(b"0" * 1024)  # truncated
        return directory

    def test_local_embed_model_env_accepts_complete_directory(self):
        model = self._fake_model(Path(self._temp.name) / "model", complete=True)
        with patch.dict(os.environ, {"LOCAL_EMBED_MODEL": str(model)}):
            status = local_index.semantic_status()
            info = local_index.embedding_info()
        self.assertTrue(status["available"])
        self.assertEqual(status["source"], "local-model-dir")
        self.assertTrue(status["local"]["complete"])
        self.assertEqual(info["local_model_path"], str(model))
        self.assertGreater(info["local_model_mib"], 1)

    def test_partial_local_model_is_reported_as_unavailable(self):
        model = self._fake_model(Path(self._temp.name) / "partial", complete=False)
        with patch.dict(os.environ, {"LOCAL_EMBED_MODEL": str(model)}):
            status = local_index.semantic_status()
            info = local_index.embedding_info()
        self.assertFalse(status["local"]["complete"])
        self.assertTrue(status["local"]["missing"])
        self.assertTrue(any("partial" in item or "MiB" in item for item in status["local"]["missing"]))
        self.assertIn("incomplete local model", status["reason"])
        self.assertEqual(info["mode"], "lexical-hashing")
        self.assertFalse(info["semantic"])

    def test_forced_semantic_mode_fails_loudly_without_model(self):
        with patch.dict(os.environ, {
            "LOCAL_INDEX_EMBED_MODE": "semantic",
            "LOCAL_EMBED_MODEL": str(Path(self._temp.name) / "absent"),
        }):
            local_index.reset_embedder()
            with self.assertRaises(RuntimeError):
                local_index.get_embedder()
            info = local_index.embedding_info()
        self.assertFalse(info["semantic"])
        self.assertEqual(info["mode"], "semantic-unavailable")
        local_index.reset_embedder()

    def test_default_semantic_model_is_not_required(self):
        status = local_index.semantic_status()
        self.assertIn("path", status)
        self.assertIn("reason", status)
        info = local_index.embedding_info()
        self.assertEqual(info["network"], "disabled")
        self.assertFalse(info["external_api"])

    @unittest.skipUnless(
        local_index._model_status()["complete"], "local bge-small model not downloaded"
    )
    def test_local_semantic_model_encodes_offline(self):
        with patch.dict(os.environ, {"LOCAL_INDEX_EMBED_MODE": "semantic"}):
            local_index.reset_embedder()
            try:
                embedder = local_index.get_embedder()
                vectors = embedder.encode([
                    "gradient descent optimization",
                    "robot arm kinematics denavit hartenberg",
                    "banana bread recipe",
                ])
            finally:
                local_index.reset_embedder()
        self.assertTrue(embedder.semantic)
        self.assertEqual(embedder.mode, "semantic")
        self.assertEqual(len(vectors[0]), 384)

        def cosine(left, right):
            dot = sum(a * b for a, b in zip(left, right))
            norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
            return dot / norm

        self.assertGreater(
            cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
        )

    def test_concurrent_embedder_initialization_builds_once(self):
        calls = []
        ready = threading.Barrier(8)
        built = object()

        def build():
            calls.append(1)
            return built

        local_index.reset_embedder()
        try:
            with patch.object(local_index, "_build_embedder", side_effect=build):
                def fetch():
                    ready.wait()
                    return local_index.get_embedder()

                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _: fetch(), range(8)))
        finally:
            local_index.reset_embedder()
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result is built for result in results))

    def test_indexing_and_search_work_with_network_disabled(self):
        def blocked(*_args, **_kwargs):
            raise OSError("network disabled in test")

        with patch.object(socket, "socket", side_effect=blocked), \
             patch.object(socket, "create_connection", side_effect=blocked):
            summary = local_index.index_all(self.root, db_path=self.db, force=True)
            results = local_index.search("chain rule derivative", k=2, db_path=self.db)
        self.assertEqual(summary["unreadable"], 1)
        self.assertGreaterEqual(len(results), 1)

    # ------------------------------------------------------------------ #
    # Corpus status accounting + coverage
    # ------------------------------------------------------------------ #
    def test_statuses_separate_searchable_empty_and_unreadable(self):
        self.assertEqual(self.summary["processed"], 6)
        self.assertEqual(self.summary["indexed"], 4)
        self.assertEqual(self.summary["searchable"], 4)
        self.assertEqual(self.summary["empty"], 1)
        self.assertEqual(self.summary["unreadable"], 1)
        self.assertEqual(self.summary["indexed_processed"], 6)
        self.assertEqual(self.summary["status_counts"], {"searchable": 4, "empty": 1, "unreadable": 1})

        by_source = {row["source"]: row for row in local_index.sources(db_path=self.db)}
        self.assertEqual(by_source["maths/blank.pdf"]["status"], "empty")
        self.assertEqual(by_source["maths/blank.pdf"]["chunks"], 0)
        self.assertEqual(by_source["maths/gone.pdf"]["status"], "unreadable")
        self.assertNotIn("maths/notes.txt", by_source)
        self.assertNotIn("notes.txt", by_source)

    def test_coverage_reports_robotics_subsection_and_keeps_empty_out_of_searchable(self):
        report = local_index.coverage(self.root, db_path=self.db)
        self.assertEqual(report["pdfs"], 6)
        self.assertEqual(report["searchable"], 4)
        self.assertEqual(report["empty"], 1)
        self.assertEqual(report["unreadable"], 1)
        self.assertEqual(report["chunks"], self.summary["chunks"])
        folders = {item["folder"]: item for item in report["folders"]}
        self.assertIn("Robotics", folders)
        self.assertTrue(folders["Robotics"]["subsection"])
        self.assertFalse(folders["Robotics"]["counted_in_total"])
        self.assertEqual(folders["Robotics"]["parent"], "Mlaidlgenai_robo")
        self.assertEqual(folders["Robotics"]["folder_path"], "Mlaidlgenai_robo/Robotics")
        self.assertEqual(folders["Robotics"]["searchable"], 1)
        self.assertEqual(folders["maths"]["searchable"], 2)
        self.assertEqual(folders["maths"]["empty"], 1)
        self.assertEqual(folders["maths"]["unreadable"], 1)

    # ------------------------------------------------------------------ #
    # Incremental behaviour
    # ------------------------------------------------------------------ #
    def test_second_run_is_incremental(self):
        again = local_index.index_all(self.root, db_path=self.db)
        self.assertEqual(again["indexed"], 0)
        self.assertEqual(again["unchanged"], 5)  # four searchable + one empty
        self.assertEqual(again["unreadable"], 1)  # missing target is retried
        self.assertEqual(again["removed"], 0)
        self.assertEqual(again["chunks"], self.summary["chunks"])

    def test_changed_pdf_replaces_rows_and_preserves_source_file(self):
        untouched = self.original / "Mlaidlgenai_robo/Robotics/robot.pdf"
        untouched_digest = _digest(untouched)
        target = self.original / "maths/algebra/basics.pdf"
        _make_pdf(target, ["Newton method uses the Hessian matrix and quadratic model."])
        written_digest = _digest(target)
        summary = local_index.index_all(self.root, db_path=self.db)
        self.assertGreaterEqual(summary["indexed"], 1)
        # Indexing must never rewrite PDFs.
        self.assertEqual(_digest(target), written_digest)
        self.assertEqual(_digest(untouched), untouched_digest)
        after = local_index.sources(db_path=self.db)
        row = next(item for item in after if item["source"] == "maths/algebra/basics.pdf")
        self.assertEqual(row["status"], "searchable")
        self.assertEqual(row["pages"], 1)

        stale = local_index.search("gradient descent alpha", k=5, sources=["maths/algebra/basics.pdf"], db_path=self.db)
        self.assertFalse(any("gradient descent" in item["text"] for item in stale), stale)
        fresh = local_index.search("Hessian quadratic", k=1, sources=["maths/algebra/basics.pdf"], db_path=self.db)
        self.assertIn("Hessian", fresh[0]["text"])

    def test_deleted_pdf_is_pruned(self):
        (self.compressed / "Thoughts/dup.pdf").unlink()
        (self.original / "Thoughts/dup.pdf").unlink()
        summary = local_index.index_all(self.root, db_path=self.db)
        self.assertEqual(summary["removed"], 1)
        names = {item["source"] for item in local_index.sources(db_path=self.db)}
        self.assertNotIn("Thoughts/dup.pdf", names)
        self.assertIn("maths/dup.pdf", names)

    def test_force_reindexes_everything(self):
        summary = local_index.index_all(self.root, db_path=self.db, force=True)
        self.assertEqual(summary["indexed"], 4)
        self.assertEqual(summary["unchanged"], 0)

    # ------------------------------------------------------------------ #
    # Search quality, citations, duplicate basenames, scopes
    # ------------------------------------------------------------------ #
    def test_page_citation_points_at_the_extracted_page(self):
        results = local_index.search("chain rule derivative", k=3, db_path=self.db)
        match = next(item for item in results if "chain rule" in item["text"])
        self.assertEqual(match["source"], "maths/algebra/basics.pdf")
        self.assertEqual(match["page"], 2)
        self.assertEqual(set(match), {"source", "page", "text", "folder", "score", "bm25", "dense"})

    def test_duplicate_basenames_stay_distinct(self):
        results = local_index.search("singular value decomposition", k=5, db_path=self.db)
        sources = {item["source"] for item in results}
        self.assertIn("maths/dup.pdf", sources)
        scoped = local_index.search("habit cue routine reward", k=1, sources=["Thoughts/dup.pdf"], db_path=self.db)
        self.assertEqual(scoped[0]["source"], "Thoughts/dup.pdf")
        self.assertIn("Habit", scoped[0]["text"])

    def test_folder_scope_and_robotics(self):
        results = local_index.search("robot arm transforms", k=3, folder="Mlaidlgenai_robo/Robotics", db_path=self.db)
        self.assertEqual(results[0]["source"], "Mlaidlgenai_robo/Robotics/robot.pdf")
        maths = local_index.search("matrix", k=3, folder="maths", db_path=self.db)
        self.assertTrue(maths)
        self.assertTrue(all(item["folder"] == "maths" for item in maths))

    def test_empty_pdfs_never_surface_as_searchable_text(self):
        results = local_index.search("blank", k=5, db_path=self.db)
        self.assertFalse(any(item["source"] == "maths/blank.pdf" for item in results))

    def test_max_chars_caps_total_context(self):
        results = local_index.search("gradient descent chain rule matrix", k=5, max_chars=60, db_path=self.db)
        self.assertLessEqual(sum(len(item["text"]) for item in results), 60)
        self.assertGreaterEqual(len(results), 1)

    def test_missing_pdf_tree_raises(self):
        with self.assertRaises(FileNotFoundError):
            local_index.index_all(Path(self._temp.name) / "absent", db_path=self.db)


if __name__ == "__main__":
    unittest.main()
