"""Tests for exact-symbol BM25 and the API-free hybrid fallback."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import store


class HybridStoreTests(unittest.TestCase):
    """Prove that sparse retrieval preserves formulas/code and works offline."""

    def test_bm25_preserves_short_code_and_math_tokens(self):
        docs = [
            {"id": "a", "source": "math.pdf", "text": "gradient descent uses x_i and alpha = 0.01"},
            {"id": "b", "source": "system.pdf", "text": "A distributed system retries requests after timeout."},
        ]
        ranked = store._bm25_rank("x_i alpha = 0.01", docs)
        self.assertEqual(ranked[0][0], "a")

    def test_keyword_fallback_works_without_gemini_or_chroma(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.json"
            with patch.object(store, "INDEX_FILE", index), \
                 patch.object(store, "_embed", return_value=None), \
                 patch.object(store, "_get_chroma_collection", return_value=None):
                store.add("math/derivation.pdf", ["The chain rule gives d/dx f(g(x)) = f'(g(x)) g'(x)."], "math")
                results = store.search("chain rule derivative", k=1)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], "math/derivation.pdf")
            self.assertIn("chain rule", results[0][1])

    def test_partial_embedding_failure_keeps_completed_vectors(self):
        completed = [[float(index)] for index in range(store.EMBED_BATCH_SIZE)]
        with patch.object(store, "_embed_dead_until", None), \
             patch.object(store.gemini, "embed", side_effect=[completed, RuntimeError("provider unavailable")]):
            vectors = store._embed([f"chunk {index}" for index in range(10)])
            self.assertIsNotNone(store._embed_dead_until)
        self.assertEqual(vectors, completed)


if __name__ == "__main__":
    unittest.main()
