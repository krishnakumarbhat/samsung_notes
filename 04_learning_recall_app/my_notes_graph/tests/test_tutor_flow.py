"""Mocked Socratic probe, rubric grading, explicit FSRS rating, and citation flow."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import memory, tutor


class SocraticFlowTests(unittest.TestCase):
    """Exercise a full no-network quiz loop against an isolated SQLite database."""

    def test_probe_grade_and_learner_selected_fsrs_rating(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            responses = [
                {
                    "question": "Derive the chain rule from a composition.",
                    "expected_points": ["composition of functions", "multiply derivatives"],
                    "hint": "Write y=f(u), u=g(x).",
                },
                {
                    "grade": 3,
                    "feedback": "Correct derivation; state the dependency explicitly.",
                    "mistake_pattern": "missing dependency statement",
                },
            ]
            with patch.object(memory, "DATA", data), \
                 patch.object(memory, "DB", data / "recall.sqlite"), \
                 patch.object(memory, "LEGACY_MEMORY_DB", data / "missing-memory.db"), \
                 patch.object(tutor, "VAULT", data / "vault"), \
                 patch.object(tutor, "_retrieved_context", return_value=([("maths/chain.pdf", "## Page 4\nchain rule notes")], [{"source": "maths/chain.pdf", "page": 4}])), \
                 patch.object(tutor.gemini, "generate_json", side_effect=responses):
                memory.set_syllabus([{"name": "Calculus", "folder": "maths", "prereq": []}])
                probe = tutor.probe()
                self.assertEqual(probe["mode"], "First-Principles Derivation")
                self.assertEqual(probe["source"]["page"], 4)

                graded = tutor.evaluate_socratic_answer(probe["id"], "For y=f(g(x)), differentiate the composition and multiply the derivatives.")
                self.assertEqual(graded["grade"], 3)
                self.assertEqual(graded["answer_id"], probe["id"])

                scheduled = tutor.apply_fsrs_rating(probe["id"], 2)
                self.assertEqual(scheduled["grade_name"], "Hard")
                self.assertGreaterEqual(scheduled["next_review_days"], 0)
                self.assertTrue(memory.history()[0]["quizzes"][0]["mistake_pattern"])
                self.assertEqual(memory.stats()[0]["total"], 1)


if __name__ == "__main__":
    unittest.main()
