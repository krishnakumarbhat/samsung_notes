"""SQLite access and schema migration for the decoupled question bank.

The study engine keeps its own tables inside ``data/recall.sqlite`` so a card's
memory state and a question's provenance travel together.  Nothing here ever
rewrites the existing ``qa``/``fsrs_cards`` tables: the migration only adds
objects, so an older process can still open the same file.

A test or a maintenance job can point ``RECALL_DB_PATH`` at a copy; the path is
therefore resolved per call instead of at import time.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from src.config import RECALL_DB

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS quiz_bank(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id TEXT NOT NULL,
    topic_folder TEXT NOT NULL DEFAULT '',
    source_pdf TEXT NOT NULL DEFAULT '',
    page_num INTEGER NOT NULL DEFAULT 0,
    cognitive_type TEXT NOT NULL CHECK(cognitive_type IN
        ('INTUITION','CALCULATION_STEP','ADVERSARIAL_TRAP','SYSTEM_APPLICATION')),
    requires_scratchpad INTEGER NOT NULL DEFAULT 0 CHECK(requires_scratchpad IN (0,1)),
    difficulty_tier INTEGER NOT NULL DEFAULT 3 CHECK(difficulty_tier BETWEEN 1 AND 5),
    question_md TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_index INTEGER NOT NULL CHECK(correct_index BETWEEN 0 AND 3),
    explanation_md TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'STAGED'
        CHECK(status IN ('STAGED','SYNCED_TO_MOBILE','ANSWERED','POSTPONED')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    dedupe_key TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL DEFAULT '',
    postponed_until REAL
);
CREATE INDEX IF NOT EXISTS quiz_bank_status_idx ON quiz_bank(status, difficulty_tier);
CREATE INDEX IF NOT EXISTS quiz_bank_concept_idx ON quiz_bank(concept_id);

CREATE TABLE IF NOT EXISTS flow_review_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER NOT NULL REFERENCES quiz_bank(id),
    user_selection INTEGER,
    is_correct INTEGER NOT NULL,
    time_spent_seconds INTEGER NOT NULL DEFAULT 0,
    flow_rating INTEGER NOT NULL CHECK(flow_rating BETWEEN 1 AND 5),
    fsrs_grade INTEGER NOT NULL CHECK(fsrs_grade BETWEEN 1 AND 4),
    context_mode TEXT NOT NULL DEFAULT 'DESK_STANDARD'
        CHECK(context_mode IN ('DESK_STANDARD','COMMUTE_TRAVEL')),
    error_category TEXT,
    synced_from_client INTEGER NOT NULL DEFAULT 0,
    reviewed_at TEXT NOT NULL,
    client_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS flow_review_quiz_idx ON flow_review_logs(quiz_id);

CREATE TABLE IF NOT EXISTS flow_state(
    session_id TEXT PRIMARY KEY,
    momentum REAL NOT NULL DEFAULT 0.0,
    target_tier INTEGER NOT NULL DEFAULT 3,
    context_mode TEXT NOT NULL DEFAULT 'DESK_STANDARD',
    answered INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS schema_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per finished game round.  ``client_id`` makes an offline retry
-- idempotent; NULL is allowed (and SQLite permits many NULLs in a UNIQUE).
CREATE TABLE IF NOT EXISTS game_scores(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL CHECK(game IN ('sudoku','trap_hunt','match')),
    topic TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0 CHECK(score >= 0),
    accuracy REAL NOT NULL DEFAULT 0 CHECK(accuracy BETWEEN 0 AND 1),
    duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK(duration_seconds >= 0),
    detail_json TEXT NOT NULL DEFAULT '{}',
    client_id TEXT UNIQUE,
    played_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS game_scores_game_idx ON game_scores(game, score DESC);
CREATE INDEX IF NOT EXISTS game_scores_topic_idx ON game_scores(topic);

-- Intra-session reinforcement: a failed question schedules a same-concept
-- variant 8-12 slots later.  ``priority`` survives the session so a concept
-- that failed twice is boosted in tomorrow's queue.
CREATE TABLE IF NOT EXISTS session_weakness(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source_quiz_id INTEGER NOT NULL,
    concept_id TEXT NOT NULL,
    variant_quiz_id INTEGER,
    insert_after INTEGER NOT NULL DEFAULT 10,
    position INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS session_weakness_session_idx
    ON session_weakness(session_id, resolved, concept_id);
CREATE INDEX IF NOT EXISTS session_weakness_variant_idx
    ON session_weakness(variant_quiz_id);

-- Leech guard: a concept that keeps lapsing. ``needs_simplified`` asks the question
-- factory for an easier intuition card when the bank has none to fall back on.
CREATE TABLE IF NOT EXISTS concept_leech(
    concept_id TEXT PRIMARY KEY,
    lapses INTEGER NOT NULL DEFAULT 0,
    needs_simplified INTEGER NOT NULL DEFAULT 0,
    flagged_at TEXT NOT NULL
);

-- Elo ratings: scope 'learner' (key '*' = overall, else a study domain) and
-- scope 'question' (key = quiz id).  Learner decay is computed on read from
-- ``last_active``, so no nightly job is needed and sync replays are safe.
CREATE TABLE IF NOT EXISTS elo_ratings(
    scope TEXT NOT NULL CHECK(scope IN ('learner','question')),
    key TEXT NOT NULL,
    rating REAL NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    last_active REAL NOT NULL,
    PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS elo_daily(
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    rating REAL NOT NULL,
    reviews INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(day, key)
);

-- Created by src.review too; repeated here so the factory CLI works on a
-- database the web app has never opened.  Additive only, identical columns.
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

UPSERTS = (
    "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('quiz_bank_schema',?)",
)


def database_path(override: str | Path | None = None) -> Path:
    """Resolve the recall database path, honouring an explicit or env override."""
    if override is not None:
        return Path(override)
    return Path(os.environ.get("RECALL_DB_PATH") or RECALL_DB)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the recall database with row access by name and WAL journaling."""
    target = database_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Create the question-bank tables and stamp their schema version once."""
    connection.executescript(SCHEMA)
    for statement in UPSERTS:
        connection.execute(statement, (str(SCHEMA_VERSION),))
    connection.commit()
    return connection


def open_database(path: str | Path | None = None) -> sqlite3.Connection:
    """Open and migrate in one call; callers own the returned connection."""
    return ensure_schema(connect(path))
