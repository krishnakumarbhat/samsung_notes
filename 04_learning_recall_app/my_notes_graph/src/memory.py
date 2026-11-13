"""Learner memory stored in a local, backward-compatible recall SQLite DB."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.config import DATA, LEGACY_MEMORY_DB, RECALL_DB

DB = RECALL_DB
_MIGRATION_LOCK = threading.RLock()


def _copy_legacy_memory() -> None:
    """Copy memory.db to recall.sqlite once; retain the original file unchanged."""
    if DB.exists():
        return
    legacy = DB.with_name("memory.db") if DB.name != "recall.sqlite" else LEGACY_MEMORY_DB
    if not legacy.is_file():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="recall-migrate-", suffix=".sqlite", dir=DATA
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source = sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(temporary)
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(destination)
        result = destination.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"legacy memory database check failed: {result}")
        destination.close()
        destination = None
        os.replace(temporary, DB)
    finally:
        source.close()
        if destination is not None:
            destination.close()
        temporary.unlink(missing_ok=True)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Create additive recall tables and migrate older interaction columns."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS qa(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, direction TEXT, topic TEXT,
            question TEXT, answer TEXT, correct INTEGER
        );
        CREATE TABLE IF NOT EXISTS mastery(
            topic TEXT PRIMARY KEY, prereq TEXT, correct INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS study_days(
            day TEXT PRIMARY KEY, first_seen REAL NOT NULL, events INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS concept_edges(
            source TEXT NOT NULL, target TEXT NOT NULL,
            PRIMARY KEY(source,target)
        );
        CREATE TABLE IF NOT EXISTS indexed_files(
            path TEXT PRIMARY KEY, source TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL, chunks INTEGER NOT NULL DEFAULT 0,
            indexed_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'indexed'
        );
        CREATE TABLE IF NOT EXISTS fsrs_cards(
            topic TEXT PRIMARY KEY, folder TEXT NOT NULL DEFAULT '', card_json TEXT NOT NULL,
            due REAL NOT NULL, stability REAL, difficulty REAL, last_review REAL,
            last_rating INTEGER, mistake_pattern TEXT NOT NULL DEFAULT ''
        );
        """
    )
    qa_columns = {row[1] for row in connection.execute("PRAGMA table_info(qa)")}
    qa_additions = {
        "grade": "INTEGER",
        "rubric": "TEXT NOT NULL DEFAULT ''",
        "feedback": "TEXT NOT NULL DEFAULT ''",
        "mistake_pattern": "TEXT NOT NULL DEFAULT ''",
        "mode": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "source_page": "INTEGER",
        "parent_id": "INTEGER",
    }
    for name, declaration in qa_additions.items():
        if name not in qa_columns:
            connection.execute(f"ALTER TABLE qa ADD COLUMN {name} {declaration}")
    mastery_columns = {row[1] for row in connection.execute("PRAGMA table_info(mastery)")}
    if "folder" not in mastery_columns:
        connection.execute("ALTER TABLE mastery ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
    indexed_columns = {row[1] for row in connection.execute("PRAGMA table_info(indexed_files)")}
    if "status" not in indexed_columns:
        connection.execute("ALTER TABLE indexed_files ADD COLUMN status TEXT NOT NULL DEFAULT 'indexed'")
    if connection.execute("SELECT COUNT(*) FROM study_days").fetchone()[0] == 0:
        for (timestamp,) in connection.execute("SELECT ts FROM qa WHERE ts IS NOT NULL"):
            day = datetime.fromtimestamp(float(timestamp)).date().isoformat()
            connection.execute(
                "INSERT OR IGNORE INTO study_days(day,first_seen,events) VALUES(?,?,1)",
                (day, float(timestamp)),
            )


def _conn() -> sqlite3.Connection:
    """Open the local recall DB and ensure its additive schema exists."""
    with _MIGRATION_LOCK:
        DATA.mkdir(parents=True, exist_ok=True)
        _copy_legacy_memory()
        connection = sqlite3.connect(DB, timeout=30)
        connection.row_factory = sqlite3.Row
        _ensure_schema(connection)
    return connection


def initialize() -> None:
    """Migrate local memory.db to recall.sqlite and ensure the current schema."""
    connection = _conn()
    connection.close()


def log_qa(
    direction: str,
    topic: str,
    question: str,
    answer: str,
    correct: int | None = None,
    *,
    grade: int | None = None,
    rubric: str = "",
    feedback: str = "",
    mistake_pattern: str = "",
    mode: str = "",
    source: str = "",
    source_page: int | None = None,
    parent_id: int | None = None,
) -> int:
    """Append a compact interaction record and return its ID."""
    with _conn() as connection:
        cursor = connection.execute(
            """INSERT INTO qa
            (ts,direction,topic,question,answer,correct,grade,rubric,feedback,mistake_pattern,mode,source,source_page,parent_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), direction, topic, question, answer, correct, grade,
             rubric, feedback, mistake_pattern, mode, source, source_page, parent_id),
        )
    return int(cursor.lastrowid)


def get_qa(qa_id: int) -> dict | None:
    """Return one interaction record for answer grading."""
    with _conn() as connection:
        row = connection.execute("SELECT * FROM qa WHERE id=?", (qa_id,)).fetchone()
    return dict(row) if row else None


def update_qa_result(
    qa_id: int,
    answer: str,
    grade: int,
    mistake_pattern: str = "",
    feedback: str = "",
) -> None:
    """Persist a learner response and rubric outcome against its probe row."""
    if not 1 <= grade <= 4:
        raise ValueError("FSRS grade must be between 1 and 4")
    with _conn() as connection:
        connection.execute(
            "UPDATE qa SET answer=?,grade=?,correct=?,mistake_pattern=?,feedback=? WHERE id=?",
            (answer, grade, int(grade >= 3), mistake_pattern, feedback, qa_id),
        )


def log_grade(qa_id: int, grade: int, mistake_pattern: str = "") -> None:
    """Update the grade and mistake classification for one saved answer."""
    if not 1 <= grade <= 4:
        raise ValueError("FSRS grade must be between 1 and 4")
    with _conn() as connection:
        connection.execute(
            "UPDATE qa SET grade=?,correct=?,mistake_pattern=? WHERE id=?",
            (grade, int(grade >= 3), mistake_pattern, qa_id),
        )


def set_syllabus(topics: list[dict]) -> None:
    """Upsert concept nodes and prerequisite edges without resetting mastery."""
    with _conn() as connection:
        for topic in topics:
            name = str(topic["name"]).strip()
            folder = str(topic.get("folder", ""))
            prerequisites = topic.get("prereq", [])
            connection.execute(
                """INSERT INTO mastery(topic,prereq,folder) VALUES(?,?,?)
                ON CONFLICT(topic) DO UPDATE SET prereq=excluded.prereq,
                    folder=CASE WHEN excluded.folder='' THEN mastery.folder ELSE excluded.folder END""",
                (name, json.dumps(prerequisites), folder),
            )
            connection.execute("DELETE FROM concept_edges WHERE source=?", (name,))
            for prerequisite in prerequisites:
                connection.execute(
                    "INSERT OR IGNORE INTO concept_edges(source,target) VALUES(?,?)",
                    (name, str(prerequisite)),
                )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('syllabus',?)",
            (json.dumps(topics),),
        )


def get_syllabus() -> list[dict] | None:
    """Return the persisted concept syllabus or None."""
    with _conn() as connection:
        row = connection.execute("SELECT value FROM meta WHERE key='syllabus'").fetchone()
    return json.loads(row["value"]) if row else None


def record_answer(topic: str, correct: bool, folder: str = "") -> None:
    """Update per-concept mastery counters without dropping historical totals."""
    with _conn() as connection:
        cursor = connection.execute(
            "UPDATE mastery SET total=total+1,correct=correct+?,folder=CASE WHEN ?='' THEN folder ELSE ? END WHERE topic=?",
            (1 if correct else 0, folder, folder, topic),
        )
        if cursor.rowcount == 0:
            connection.execute(
                "INSERT INTO mastery(topic,prereq,correct,total,folder) VALUES(?,?,?,?,?)",
                (topic, "[]", int(correct), 1, folder),
            )


def stats() -> list[dict]:
    """Return per-topic mastery, prerequisite links, folder and mastered flag."""
    with _conn() as connection:
        rows = connection.execute("SELECT * FROM mastery ORDER BY topic").fetchall()
    output = []
    for row in rows:
        correct, total = int(row["correct"] or 0), int(row["total"] or 0)
        percent = round(100 * correct / total) if total else 0
        output.append(
            {
                "topic": row["topic"],
                "prereq": json.loads(row["prereq"] or "[]"),
                "folder": row["folder"] if "folder" in row.keys() else "",
                "correct": correct,
                "total": total,
                "pct": percent,
                "mastered": total >= 3 and percent >= 80,
            }
        )
    return output


def next_probe_topic() -> tuple[str, bool] | None:
    """Select due FSRS cards first, then untested or weakest prerequisite-ready concepts."""
    from src import review as scheduler

    due = scheduler.due_topics(limit=1)
    if due:
        return due[0]["topic"], True
    all_stats = {item["topic"]: item for item in stats()}
    ready = [
        item for item in all_stats.values()
        if all(all_stats[name]["pct"] >= 60 for name in item["prereq"] if name in all_stats)
    ] or list(all_stats.values())
    if not ready:
        return None
    ready.sort(key=lambda item: (0 if item["total"] == 0 else 1, item["pct"], item["topic"]))
    return ready[0]["topic"], False


def learner_profile(max_topics: int = 4) -> str:
    """Build a compact profile suitable for a small model prompt budget."""
    topics = sorted(stats(), key=lambda item: (item["pct"], item["topic"]))
    weak = [f"{item['topic']}({item['pct']}%)" for item in topics if item["total"] and item["pct"] < 70][:max_topics]
    untested = [item["topic"] for item in topics if not item["total"]][:max_topics]
    strong = [f"{item['topic']}({item['pct']}%)" for item in reversed(topics) if item["mastered"]][:3]
    study = study_profile()
    parts = [f"inactive_days={study['inactive_days']}", f"daily_cap={study['daily_cap']}"]
    if untested:
        parts.append("untested=" + ",".join(untested))
    if weak:
        parts.append("weak=" + ",".join(weak))
    if strong:
        parts.append("strong=" + ",".join(strong))
    return "; ".join(parts)[:700]


def qa_count() -> int:
    """Return the number of stored question/answer interactions."""
    with _conn() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM qa").fetchone()[0])


def history(limit_days: int = 60) -> list[dict]:
    """Return daily question, answer, grade and mistake history, newest first."""
    with _conn() as connection:
        rows = connection.execute(
            """SELECT ts,direction,topic,question,answer,correct,grade,feedback,mistake_pattern,mode,source,source_page
            FROM qa ORDER BY ts DESC LIMIT 1200"""
        ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d")
        item = grouped.setdefault(
            day, {"date": day, "asked": [], "quizzes": [], "correct": 0, "mistakes": {}}
        )
        if row["direction"] == "asked":
            item["asked"].append({"topic": row["topic"], "question": row["question"],
                                  "answer": row["answer"], "source": row["source"],
                                  "source_page": row["source_page"]})
        elif row["direction"] in ("probed", "answered"):
            quiz = {
                "topic": row["topic"], "question": row["question"],
                "answer": row["answer"], "correct": bool(row["correct"]),
                "grade": row["grade"], "mistake_pattern": row["mistake_pattern"],
                "feedback": row["feedback"], "mode": row["mode"],
                "source": row["source"], "source_page": row["source_page"],
            }
            quiz["pending"] = row["correct"] is None and row["grade"] is None
            if quiz["pending"]:
                try:
                    legacy = json.loads(quiz["answer"] or "{}")
                    if isinstance(legacy, dict) and "expected_points" in legacy:
                        quiz["answer"] = "Legacy ungraded probe; private rubric hidden."
                except (ValueError, TypeError):
                    pass
            item["quizzes"].append(quiz)
            if row["correct"]:
                item["correct"] += 1
            elif row["mistake_pattern"]:
                pattern = row["mistake_pattern"]
                item["mistakes"][pattern] = item["mistakes"].get(pattern, 0) + 1
    return sorted(grouped.values(), key=lambda item: item["date"], reverse=True)[:limit_days]


def record_study_day() -> dict:
    """Persist today's activity and return the full calendar-day inactivity gap."""
    today = date.today()
    today_text = today.isoformat()
    with _conn() as connection:
        seen_today = connection.execute(
            "SELECT events FROM study_days WHERE day=?", (today_text,)
        ).fetchone()
        previous = connection.execute(
            "SELECT MAX(day) FROM study_days WHERE day<?", (today_text,)
        ).fetchone()[0]
        if seen_today:
            connection.execute(
                "UPDATE study_days SET events=events+1 WHERE day=?", (today_text,)
            )
            prior_gap = connection.execute(
                "SELECT value FROM meta WHERE key='last_inactivity_days'"
            ).fetchone()
            inactive = int(prior_gap[0]) if prior_gap else 0
        else:
            inactive = (
                max(0, (today - date.fromisoformat(previous)).days - 1)
                if previous
                else 0
            )
            connection.execute(
                "INSERT INTO study_days(day,first_seen,events) VALUES(?,?,1)",
                (today_text, time.time()),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('last_inactivity_days',?)",
                (str(inactive),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('last_inactivity_date',?)",
                (today_text,),
            )
    return {"today": today_text, "previous_study_day": previous, "inactive_days": inactive}


def study_profile() -> dict:
    """Return calendar inactivity and daily review capacity."""
    from src.config import DAILY_REVIEW_CAP

    with _conn() as connection:
        last = connection.execute("SELECT MAX(day) FROM study_days").fetchone()[0]
        last_gap = connection.execute(
            "SELECT value FROM meta WHERE key='last_inactivity_days'"
        ).fetchone()
        gap_day = connection.execute(
            "SELECT value FROM meta WHERE key='last_inactivity_date'"
        ).fetchone()
    if gap_day and gap_day[0] == date.today().isoformat() and last_gap:
        inactive = int(last_gap[0])
    else:
        inactive = max(0, (date.today() - date.fromisoformat(last)).days) if last else 0
    capacity = max(1, min(DAILY_REVIEW_CAP, max(5, DAILY_REVIEW_CAP - inactive // 3)))
    return {
        "last_study_day": last,
        "inactive_days": inactive,
        "daily_cap": capacity,
    }


def mistake_patterns(limit: int = 20) -> list[dict]:
    """Aggregate recent rubric mistake classifications."""
    with _conn() as connection:
        rows = connection.execute(
            """SELECT mistake_pattern,COUNT(*) AS count FROM qa
            WHERE mistake_pattern!='' GROUP BY mistake_pattern ORDER BY count DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"pattern": row["mistake_pattern"], "count": row["count"]} for row in rows]


def concept_edges() -> list[dict]:
    """Return prerequisite graph edges for the dashboard and retrieval booster."""
    with _conn() as connection:
        rows = connection.execute(
            "SELECT source,target FROM concept_edges ORDER BY source,target"
        ).fetchall()
    return [{"from": row["source"], "to": row["target"]} for row in rows]


def latest_mistake(topic: str) -> str:
    """Return the latest error classification for a concept, if any."""
    with _conn() as connection:
        row = connection.execute(
            "SELECT mistake_pattern FROM qa WHERE topic=? AND mistake_pattern!='' ORDER BY ts DESC LIMIT 1",
            (topic,),
        ).fetchone()
    return str(row["mistake_pattern"]) if row else ""
