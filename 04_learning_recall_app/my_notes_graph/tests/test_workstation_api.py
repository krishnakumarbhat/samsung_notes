"""HTTP validation, source containment, and failure behavior with no real model requests."""
from __future__ import annotations
import importlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from src import learning, memory
from src.gemini_client import GeminiError


class WorkstationApiTests(unittest.TestCase):
    """Use an isolated learner DB and never record artificial study events in real history."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        root = Path(self.temp.name)
        for key, value in (("DATA", root), ("DB", root / "recall.sqlite"), ("LEGACY_MEMORY_DB", root / "missing.db")):
            self.stack.enter_context(patch.object(memory, key, value))
        self.module = importlib.import_module("00_main")
        self.stack.enter_context(patch.object(self.module.local_index, "sources", return_value=[]))
        self.stack.enter_context(patch.object(self.module.ingest, "coverage", return_value={"folders": []}))
        self.client = self.stack.enter_context(TestClient(self.module.app))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_health_security_headers_and_host_allowlist(self):
        result = self.client.get("/api/health")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["retrieval"], "local-only")
        self.assertIn("default-src 'self'", result.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/api/health", headers={"host": "attacker.invalid"}).status_code, 400)

    def test_invalid_exam_parameters_rejected_before_generation(self):
        with patch.object(learning, "exam_start") as generate:
            result = self.client.post("/api/exam/start", json={"count": 26})
        self.assertEqual(result.status_code, 422)
        generate.assert_not_called()

    def test_provider_failure_explicit_and_no_fake_quiz(self):
        with patch.object(learning, "exam_start", side_effect=GeminiError("provider unavailable")):
            result = self.client.post("/api/exam/start", json={"count": 1})
        self.assertEqual(result.status_code, 503)
        self.assertIn("provider unavailable", result.json()["detail"])

    def test_source_viewer_prefers_derived_and_denies_traversal(self):
        root = Path(self.temp.name) / "notes"
        (root / "compressed/maths").mkdir(parents=True)
        (root / "compressed/maths/sample.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        with patch.object(self.module, "NOTES_DIR", root):
            response = self.client.get("/source", params={"path": "maths/sample.pdf", "page": 2})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertEqual(self.client.get("/source", params={"path": "../../.env"}).status_code, 404)

    def test_empty_tutor_prompt_never_calls_provider(self):
        with patch.object(learning.gemini, "generate_json") as call:
            response = self.client.post("/api/tutor/step", json={})
        self.assertEqual(response.status_code, 422)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
