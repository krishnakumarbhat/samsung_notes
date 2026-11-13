"""LAN batch pull and review push simulation against a throwaway database."""
from __future__ import annotations

import json

from engine import sync_bridge
from tests.bank_fixtures import add_question, make_db, sqlite_rows


def seed(path, count: int = 120) -> None:
    for index in range(count):
        add_question(path, concept=f"concept {index}", folder="maths/03. Algebra",
                     cognitive="INTUITION" if index % 2 else "CALCULATION_STEP",
                     text=f"Question number {index} about $x_{{{index}}}$.")


def test_pull_batch_is_compact_and_marks_questions_synced(tmp_path):
    """A 100-question LAN batch stays under 1.5 MB and is ready for a phone."""
    path = make_db(tmp_path)
    seed(path)
    payload = sync_bridge.pull_batch(count=100, path=str(path))
    assert payload["count"] == 100
    assert payload["bytes"] <= sync_bridge.MAX_BATCH_BYTES
    assert payload["deck_id"]
    assert all("correct_index" in question for question in payload["questions"])
    rows = sqlite_rows(path, "SELECT status, COUNT(*) AS n FROM quiz_bank GROUP BY status")
    assert {row["status"]: row["n"] for row in rows} == {"STAGED": 20,
                                                         "SYNCED_TO_MOBILE": 100}


def test_push_reviews_updates_logs_and_marks_answered(tmp_path):
    """Five device reviews land in SQLite, advance FSRS and close the questions."""
    path = make_db(tmp_path)
    seed(path, count=8)
    deck = sync_bridge.pull_batch(count=5, path=str(path))
    reviews = [{
        "quiz_id": question["id"],
        "user_selection": question["correct_index"],
        "flow_rating": 3,
        "client_id": f"phone-{question['id']}",
    } for question in deck["questions"]]
    result = sync_bridge.push_reviews(reviews, path=str(path), session_id="phone")
    assert result["accepted"] == 5
    assert result["duplicates"] == 0
    assert len(sqlite_rows(path, "SELECT id FROM flow_review_logs")) == 5
    answered = sqlite_rows(path, "SELECT COUNT(*) AS n FROM quiz_bank WHERE status='ANSWERED'")
    assert answered[0]["n"] == 5
    assert len(sqlite_rows(path, "SELECT topic FROM fsrs_cards")) == 5


def test_retrying_the_same_client_id_is_idempotent(tmp_path):
    """A phone that retries after a dropped response cannot double-count."""
    path = make_db(tmp_path)
    seed(path, count=3)
    deck = sync_bridge.pull_batch(count=1, path=str(path))
    question = deck["questions"][0]
    review = {"quiz_id": question["id"], "user_selection": 0, "flow_rating": 4,
              "client_id": "retry-1"}
    first = sync_bridge.push_reviews([review], path=str(path))
    second = sync_bridge.push_reviews([review], path=str(path))
    assert first["accepted"] == 1
    assert second["accepted"] == 0 and second["duplicates"] == 1
    assert len(sqlite_rows(path, "SELECT id FROM flow_review_logs")) == 1
