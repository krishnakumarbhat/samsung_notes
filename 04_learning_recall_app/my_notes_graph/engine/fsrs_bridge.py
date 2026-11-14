"""Bridge between the quiz bank and the pinned FSRS-v5 scheduler.

The question bank stores *questions*; memory state stays on the ``fsrs_cards``
row of the concept the question belongs to.  Two operations are needed:

* a completed review updates stability/difficulty exactly like a normal recall
  rating, and
* travel mode postpones a card by 24 hours **without** recording a lapse, so a
  commute never damages the memory model.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating

from src.review import SCHEDULER

from . import db

DAY = 86400.0

FLOW_TO_GRADE = {1: 4, 2: 3, 3: 3, 4: 2, 5: 1}
FLOW_LABELS = {
    1: "Too Easy",
    2: "Comfortable",
    3: "Flow State",
    4: "Challenging",
    5: "Overwhelmed",
}


def grade_for_flow(flow_rating: int) -> int:
    """Map the five-point subjective rating onto the four FSRS grades."""
    if isinstance(flow_rating, bool) or not isinstance(flow_rating, int):
        raise ValueError("flow_rating must be an integer between 1 and 5")
    try:
        return FLOW_TO_GRADE[flow_rating]
    except KeyError:
        raise ValueError("flow_rating must be an integer between 1 and 5") from None


def grade_for_review(flow_rating: int, correct: bool) -> int:
    """FSRS grade for an answered question.

    The flow rating says how the question *felt*; it must not override the
    outcome.  A wrong answer is always ``Again`` (1) so that "wrong but it felt
    easy" can never lengthen the interval, and a correct answer that left the
    learner overwhelmed is ``Hard`` (2), never a lapse.
    """
    grade = grade_for_flow(flow_rating)
    if not correct:
        return 1
    return max(2, grade)


def _load_card(connection: sqlite3.Connection, topic: str) -> tuple[Card, str, int | None]:
    row = connection.execute(
        "SELECT card_json,folder,last_rating FROM fsrs_cards WHERE topic=?", (topic,)
    ).fetchone()
    if not row:
        return Card(), "", None
    return (Card.from_dict(json.loads(row["card_json"])), str(row["folder"] or ""),
            row["last_rating"])


def _save_card(connection: sqlite3.Connection, topic: str, folder: str,
               card: Card, rating: int | None) -> None:
    connection.execute(
        """INSERT OR REPLACE INTO fsrs_cards
        (topic,folder,card_json,due,stability,difficulty,last_review,last_rating)
        VALUES(?,?,?,?,?,?,?,?)""",
        (
            topic,
            folder,
            json.dumps(card.to_dict(), separators=(",", ":")),
            card.due.timestamp(),
            card.stability,
            card.difficulty,
            card.last_review.timestamp() if card.last_review else None,
            rating,
        ),
    )


def apply_review(topic: str, grade: int, *, folder: str = "",
                 mistake_pattern: str = "", reviewed_at: datetime | None = None,
                 path: str | None = None) -> dict:
    """Apply one FSRS grade to the concept card and append an fsrs_logs row."""
    now = reviewed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("reviewed_at must be timezone-aware")
    connection = db.open_database(path)
    try:
        card, previous_folder, _last_rating = _load_card(connection, topic)
        updated, log = SCHEDULER.review_card(card, Rating(int(grade)), review_datetime=now)
        _save_card(connection, topic, folder or previous_folder, updated, int(grade))
        connection.execute(
            """INSERT INTO fsrs_logs
            (topic,card_id,rating,review_at,due,stability,difficulty,mistake_pattern)
            VALUES(?,?,?,?,?,?,?,?)""",
            (topic, int(log.card_id), int(log.rating), log.review_datetime.timestamp(),
             updated.due.timestamp(), updated.stability, updated.difficulty,
             mistake_pattern or ""),
        )
        connection.commit()
        return {
            "topic": topic,
            "grade": int(grade),
            "due": updated.due.isoformat(),
            "stability": updated.stability,
            "difficulty": updated.difficulty,
            "next_review_days": max(0.0, (updated.due - now).total_seconds() / DAY),
        }
    finally:
        connection.close()


def postpone(topic: str, hours: float = 24.0, *, path: str | None = None,
             now: datetime | None = None) -> dict:
    """Move a card's due time forward without recording any review or lapse.

    Stability, difficulty and the review history are deliberately untouched:
    a postponed question is *not* evidence that the learner forgot it.
    """
    moment = now or datetime.now(timezone.utc)
    connection = db.open_database(path)
    try:
        card, folder, last_rating = _load_card(connection, topic)
        target = max(card.due, moment) + timedelta(hours=hours)
        card.due = target
        _save_card(connection, topic, folder, card, last_rating)
        connection.commit()
        return {"topic": topic, "due": card.due.isoformat(),
                "stability": card.stability, "difficulty": card.difficulty}
    finally:
        connection.close()


def card_state(topic: str, path: str | None = None) -> dict | None:
    """Return the persisted memory state of a concept, if it has one."""
    connection = db.open_database(path)
    try:
        row = connection.execute(
            "SELECT topic,folder,due,stability,difficulty,last_rating FROM fsrs_cards WHERE topic=?",
            (topic,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def weak_concepts(limit: int = 30, path: str | None = None) -> list[dict]:
    """Rank concepts by fragility: low stability first, then low difficulty margin."""
    connection = db.open_database(path)
    try:
        rows = connection.execute(
            """SELECT topic,folder,due,COALESCE(stability,0) AS stability,
                      COALESCE(difficulty,0) AS difficulty
               FROM fsrs_cards
               ORDER BY COALESCE(stability,0) ASC, due ASC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
