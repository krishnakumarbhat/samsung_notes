"""Shared builders for the decoupled question-bank tests.

Every test owns a throwaway SQLite file and passes it explicitly (or through
``RECALL_DB_PATH`` for the HTTP layer), so the live study database is never
touched and tests can run in any order.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

FSRS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fsrs_cards(
    topic TEXT PRIMARY KEY,
    folder TEXT NOT NULL DEFAULT '',
    card_json TEXT NOT NULL,
    due REAL NOT NULL,
    stability REAL,
    difficulty REAL,
    last_review REAL,
    last_rating INTEGER
);
CREATE TABLE IF NOT EXISTS fsrs_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    card_id INTEGER,
    rating INTEGER,
    review_at REAL,
    due REAL,
    stability REAL,
    difficulty REAL,
    mistake_pattern TEXT
);
"""

TYPES = ("INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "SYSTEM_APPLICATION")


def make_db(directory) -> Path:
    """Create a temporary recall database with the FSRS tables the engine reads."""
    from engine import db

    target = Path(directory) / "recall.sqlite"
    connection = db.open_database(target)
    connection.executescript(FSRS_SCHEMA)
    connection.commit()
    connection.close()
    return target


def add_card(path, topic: str = "Algebra", *, folder: str = "maths",
             stability: float = 1.0, due_in_hours: float = 0.0) -> None:
    """Insert an FSRS card row without going through the scheduler."""
    from engine import db

    due = datetime.now(timezone.utc) + timedelta(hours=due_in_hours)
    card = {
        "card_id": 1,
        "state": 2,
        "step": None,
        "stability": stability,
        "difficulty": 5.0,
        "due": due.isoformat(),
        "last_review": (due - timedelta(days=1)).isoformat(),
    }
    connection = db.open_database(path)
    try:
        connection.execute(
            """INSERT OR REPLACE INTO fsrs_cards
            (topic,folder,card_json,due,stability,difficulty,last_review,last_rating)
            VALUES(?,?,?,?,?,?,?,?)""",
            (topic, folder, json.dumps(card), due.timestamp(), stability, 5.0,
             (due - timedelta(days=1)).timestamp(), 3),
        )
        connection.commit()
    finally:
        connection.close()


def add_question(path, *, concept: str = "Algebra", cognitive: str = "INTUITION",
                 scratchpad: int | None = None, tier: int = 3, correct: int = 0,
                 text: str = "What is the rank of $A$?", options=None,
                 status: str = "STAGED", postponed_until: float | None = None,
                 folder: str = "maths", source: str = "maths/algebra.pdf",
                 explanation: str = "Because $\\operatorname{rank} A = 2$.") -> int:
    """Insert one bank row and return its id."""
    from engine import db

    options = options or ["1", "2", "3", "4"]
    if scratchpad is None:
        scratchpad = 1 if cognitive == "CALCULATION_STEP" else 0
    key = hashlib.sha256(f"{concept}|{text}".encode()).hexdigest()
    connection = db.open_database(path)
    try:
        cursor = connection.execute(
            """INSERT INTO quiz_bank
            (concept_id,topic_folder,source_pdf,page_num,cognitive_type,
             requires_scratchpad,difficulty_tier,question_md,options_json,
             correct_index,explanation_md,status,dedupe_key,postponed_until)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (concept, folder, source, 1, cognitive, int(scratchpad), int(tier), text,
             json.dumps(options), int(correct), explanation, status, key,
             postponed_until),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def bulk_questions(path, count: int, *, concept: str = "Algebra",
                   cognitive: str = "INTUITION", filler: int = 40,
                   status: str = "STAGED") -> None:
    """Insert many bank rows in one transaction (deck-size tests)."""
    from engine import db

    rows = []
    for index in range(count):
        text = f"Question {index} " + ("y" * filler) + " $x$?"
        key = hashlib.sha256(f"{concept}|{text}".encode()).hexdigest()
        rows.append((
            concept, "maths", "maths/algebra.pdf", 1, cognitive,
            1 if cognitive == "CALCULATION_STEP" else 0, 3, text,
            json.dumps(["a", "b", "c", "d"]), 0,
            "Explanation " + ("z" * filler) + " $y$", status, key,
        ))
    connection = db.open_database(path)
    try:
        connection.executemany(
            """INSERT INTO quiz_bank
            (concept_id,topic_folder,source_pdf,page_num,cognitive_type,
             requires_scratchpad,difficulty_tier,question_md,options_json,
             correct_index,explanation_md,status,dedupe_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def batch_payload(count: int = 5, *, concept: str = "Algebra") -> dict:
    """A schema-valid model response following the cognitive rotation."""
    questions = []
    for index in range(count):
        cognitive = TYPES[index % len(TYPES)]
        questions.append({
            "cognitive_type": cognitive,
            "difficulty_tier": 2 + (index % 3),
            "question_md": f"{concept} check {index}: evaluate $x^{{{index}}}$.",
            "options": [f"$x^{{{index}}}$", f"$x^{{{index + 1}}}$",
                        f"$\\frac{{1}}{{x^{{{index}}}}}$", f"$0$"],
            "correct_index": index % 4,
            "explanation_md": f"Use the power rule; the answer is $x^{{{index}}}$.",
        })
    return {"questions": questions}


class FakeClient:
    """Duck-typed stand-in for :class:`engine.gemini_batch.BatchGemini`."""

    def __init__(self, *payloads):
        self.payloads = list(payloads) or [batch_payload()]
        self.calls = []

    def json(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        item = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        if isinstance(item, Exception):
            raise item
        return item


class DummyTarget:
    """Minimal target object accepted by the prompt builder."""

    concept_id = "Algebra"
    folder = "maths"
    source_pdf = "maths/algebra.pdf"
    page_num = 1
    reason = "test"


def now() -> datetime:
    return datetime.now(timezone.utc)


def sqlite_rows(path, query: str, params=()):
    """Read rows from a test database without importing the engine."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params)]
    finally:
        connection.close()
