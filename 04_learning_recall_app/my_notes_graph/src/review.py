"""FSRS-v5 review scheduling with a bounded daily queue and retention estimate."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler, State
from fsrs.fsrs import DECAY, FACTOR

from src import memory
from src.config import DAILY_REVIEW_CAP

DAY = 86400.0
RETENTION_FACTOR = FACTOR
SCHEDULER = Scheduler(enable_fuzzing=True)


def _conn() -> sqlite3.Connection:
    """Open recall.sqlite, add FSRS tables, and lazily migrate legacy SM-2 rows."""
    connection = memory._conn()
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
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
            card_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            review_at REAL NOT NULL,
            due REAL NOT NULL,
            stability REAL,
            difficulty REAL,
            mistake_pattern TEXT NOT NULL DEFAULT ''
        );
        """
    )
    _migrate_legacy_sm2(connection)
    return connection


def _migrate_legacy_sm2(connection: sqlite3.Connection) -> None:
    """Keep old SM-2 rows and seed equivalent FSRS cards once."""
    if connection.execute("SELECT 1 FROM meta WHERE key='fsrs_v5_migrated'").fetchone():
        return
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "review" in tables:
        for row in connection.execute("SELECT topic,ease,interval,reps,due FROM review"):
            topic, ease, interval, reps, due = row
            if connection.execute("SELECT 1 FROM fsrs_cards WHERE topic=?", (topic,)).fetchone():
                continue
            if reps and interval and interval > 0:
                due_at = datetime.fromtimestamp(float(due), timezone.utc)
                card = Card(
                    state=State.Review,
                    step=None,
                    stability=max(float(interval), 0.1),
                    difficulty=max(1.0, min(10.0, 5.0 + (2.5 - float(ease)) * 2.0)),
                    due=due_at,
                    last_review=due_at - timedelta(days=float(interval)),
                )
            else:
                card = Card()
            _save_card(connection, topic, "", card, None, "")
    connection.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('fsrs_v5_migrated','1')"
    )
    connection.commit()


def _save_card(
    connection: sqlite3.Connection,
    topic: str,
    folder: str,
    card: Card,
    rating: int | None,
    mistake_pattern: str,
) -> None:
    """Persist the FSRS card JSON and indexed due/stability columns."""
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


def _load_card(connection: sqlite3.Connection, topic: str) -> tuple[Card, str]:
    """Load an existing FSRS card or create a new immediately-due card."""
    row = connection.execute(
        "SELECT card_json,folder FROM fsrs_cards WHERE topic=?", (topic,)
    ).fetchone()
    if not row:
        return Card(), ""
    return Card.from_dict(json.loads(row["card_json"])), str(row["folder"] or "")


def retrievability(card: Card, now: datetime | None = None) -> float:
    """Use the pinned FSRS-v5 forgetting curve, not a separately tuned proxy."""
    if card.stability is None or card.last_review is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    elapsed_days = max(0.0, (now - card.last_review).total_seconds() / DAY)
    stability = max(float(card.stability), 0.01)
    return (1.0 + RETENTION_FACTOR * elapsed_days / stability) ** DECAY


def update(
    topic: str,
    rating: int | bool,
    folder: str = "",
    mistake_pattern: str = "",
    review_datetime: datetime | None = None,
) -> dict:
    """Apply FSRS-v5 Rating Again/Hard/Good/Easy (1–4) and persist the new card."""
    selected = Rating.Good if rating is True else Rating.Again if rating is False else Rating(int(rating))
    now = review_datetime or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("review_datetime must be timezone-aware")
    with _conn() as connection:
        card, previous_folder = _load_card(connection, topic)
        updated, review_log = SCHEDULER.review_card(card, selected, review_datetime=now)
        target_folder = folder or previous_folder
        _save_card(connection, topic, target_folder, updated, int(selected), mistake_pattern)
        connection.execute(
            """INSERT INTO fsrs_logs
            (topic,card_id,rating,review_at,due,stability,difficulty,mistake_pattern)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                topic,
                int(review_log.card_id),
                int(review_log.rating),
                review_log.review_datetime.timestamp(),
                updated.due.timestamp(),
                updated.stability,
                updated.difficulty,
                mistake_pattern,
            ),
        )
    return {
        "rating": int(selected),
        "due": updated.due.isoformat(),
        "next_review_days": max(0.0, (updated.due - now).total_seconds() / DAY),
        "stability": updated.stability,
        "difficulty": updated.difficulty,
        "retrievability": retrievability(updated, now),
    }


def due_topics(limit: int | None = None) -> list[dict]:
    """Return the lowest-retrievability due cards, capped by the daily capacity."""
    cap = max(1, min(int(limit or DAILY_REVIEW_CAP), DAILY_REVIEW_CAP))
    now = datetime.now(timezone.utc)
    now_timestamp = now.timestamp()
    with _conn() as connection:
        rows = connection.execute(
            "SELECT topic,folder,card_json,due FROM fsrs_cards WHERE due<=?",
            (now_timestamp,),
        ).fetchall()
    due = []
    for row in rows:
        card = Card.from_dict(json.loads(row["card_json"]))
        retention = retrievability(card, now)
        due.append(
            {
                "topic": row["topic"],
                "folder": row["folder"],
                "due": row["due"],
                "overdue_h": round(max(0.0, now_timestamp - row["due"]) / 3600),
                "retrievability": round(retention, 4),
                "stability": card.stability,
            }
        )
    due.sort(key=lambda item: (item["retrievability"], -item["overdue_h"], item["topic"]))
    return due[:cap]


def next_interval_days(topic: str) -> float:
    """Return the remaining due interval in days, including short learning steps."""
    with _conn() as connection:
        row = connection.execute("SELECT due FROM fsrs_cards WHERE topic=?", (topic,)).fetchone()
    if not row:
        return 0.0
    return max(0.0, (float(row["due"]) - datetime.now(timezone.utc).timestamp()) / DAY)
