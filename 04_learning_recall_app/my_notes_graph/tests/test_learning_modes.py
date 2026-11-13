"""No-network pedagogical flows, exam confidentiality, and idempotent scheduling."""
from __future__ import annotations
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from src import learning, memory


class LearningModesTests(unittest.TestCase):
    """Store all observations in a temporary learner DB; mock model output."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        data = Path(self.temp.name)
        for key, value in (("DATA", data), ("DB", data / "recall.sqlite"), ("LEGACY_MEMORY_DB", data / "missing.db")):
            self.stack.enter_context(patch.object(memory, key, value))
        self.stack.enter_context(patch.object(learning, "snippets", return_value=[
            {"source": "maths/derivatives.pdf", "page": 7, "text": "The derivative of x squared is twice x."}]))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def question(self):
        return {"questions": [{"question": "Differentiate x² at x=3.", "options": ["3", "6", "9", "12"],
                               "correct_index": 1, "concept": "Derivative", "explanation": "2x=6.",
                               "source_index": 1, "error_tag": "Algebraic/Calculation Slip"}]}

    def test_exam_keys_stay_private_until_finish_and_finish_is_idempotent(self):
        with patch.object(learning.gemini, "generate_json", return_value=self.question()):
            exam = learning.exam_start(["maths/derivatives.pdf"], "exam", 1, 45)
        self.assertNotIn("correct_index", exam["questions"][0])
        self.assertNotIn("explanation", exam["questions"][0])
        answer = learning.exam_answer(exam["id"], 1, 1, False)
        self.assertNotIn("feedback", answer)
        self.assertNotIn("correct_index", str(learning.rich_history()))
        result = learning.exam_finish(exam["id"])
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["results"][0]["source"]["page"], 7)
        self.assertEqual(learning.exam_finish(exam["id"]), result)
        with learning.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM fsrs_logs").fetchone()[0], 1)

    def test_practice_instant_feedback_clear_mark_and_server_deadline(self):
        with patch.object(learning.gemini, "generate_json", return_value=self.question()):
            exam = learning.exam_start([], "practice", 1, 45)
        self.assertFalse(learning.exam_answer(exam["id"], 1, 0, True)["feedback"]["correct"])
        self.assertNotIn("feedback", learning.exam_answer(exam["id"], 1, None, True))
        data = learning.load_session(exam["id"], "exam")
        self.assertIsNone(data["questions"][0]["selected"])
        data.update(mode="exam", deadline=time.time() - 1)
        learning.save_session(exam["id"], "exam", data)
        with self.assertRaisesRegex(ValueError, "expired"):
            learning.exam_answer(exam["id"], 1, 1, False)

    def test_tutor_reuses_private_rubric_and_changes_analogy_prompt(self):
        turn = {"explanation": "Think of a speedometer.", "question": "What is the rate at x=3?",
                "expected": "6", "correct": None, "feedback": ""}
        with patch.object(learning.gemini, "generate_json", return_value=turn):
            first = learning.tutor_step(None, ["maths/derivatives.pdf"], "teach derivatives")
        self.assertNotIn("expected", first)
        with patch.object(learning.gemini, "generate_json", return_value={**turn, "correct": False,
                          "explanation": "Now picture filling a bucket.", "error_tag": "Forgot Formula"}) as call:
            second = learning.tutor_step(first["session_id"], [], "9")
        self.assertIn("ENTIRELY NEW analogy", call.call_args.args[0])
        self.assertIn("6", call.call_args.args[0])
        self.assertNotEqual(second["explanation"], first["explanation"])
        self.assertEqual(learning.intelligence()["observations"], 1)

    def test_twenty_five_question_canvas_is_generated_in_five_validated_batches(self):
        responses = []
        for batch in range(5):
            questions = []
            for index in range(5):
                item = dict(self.question()["questions"][0])
                item["question"] += f" Scenario {batch*5+index+1}."
                questions.append(item)
            responses.append({"questions": questions})
        with patch.object(learning.gemini, "generate_json", side_effect=responses) as generate:
            exam = learning.exam_start([], "exam", 25, 45)
        self.assertEqual(len(exam["questions"]), 25)
        self.assertEqual(generate.call_count, 5)
        self.assertEqual([row["id"] for row in exam["questions"]], list(range(1,26)))
        self.assertTrue(all("correct_index" not in row for row in exam["questions"]))
        self.assertEqual(generate.call_args.kwargs["response_schema"]["properties"]["questions"]["maxItems"], 5)


class SourceScopeTests(unittest.TestCase):
    """UI folder/note tokens must resolve to exact local source names, not an empty scope."""

    def test_folder_and_note_tokens_expand_to_exact_sources(self):
        from src import local_index
        sources = [{"source": "maths/a.pdf"}, {"source": "Thoughts/b.pdf"}]
        hit = {"source": "maths/a.pdf", "page": 1, "text": "A derivative"}
        with patch.object(local_index, "sources", return_value=sources), \
             patch.object(local_index, "search", return_value=[hit]) as search:
            self.assertEqual(learning.snippets("derivative", ["folder:maths"]), [hit])
            self.assertEqual(search.call_args.kwargs["sources"], ["maths/a.pdf"])
            learning.snippets("derivative", ["note:maths/a.pdf"])
            self.assertEqual(search.call_args.kwargs["sources"], ["maths/a.pdf"])


if __name__ == "__main__":
    unittest.main()
