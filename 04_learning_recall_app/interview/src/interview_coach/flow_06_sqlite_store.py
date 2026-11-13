from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import Difficulty, InterviewRecord, InterviewSection


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SQLiteInterviewStore:
    def __init__(self, settings: AppSettings) -> None:
        self._path = settings.sqlite_path
        self._initialize()

    def save_record(self, record: InterviewRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO interview_records (
                    record_id,
                    section,
                    question,
                    question_hash,
                    rationale,
                    topics_json,
                    difficulty,
                    source_refs_json,
                    answer_text,
                    answer_summary,
                    score,
                    strengths_json,
                    gaps_json,
                    coaching_note,
                    follow_up,
                    asked_at,
                    due_at,
                    repetition_count,
                    interval_days,
                    easiness,
                    related_record_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.section.value,
                    record.question,
                    record.question_hash,
                    record.rationale,
                    json.dumps(record.topics),
                    record.difficulty.value,
                    json.dumps(record.source_inspiration),
                    record.answer_text,
                    record.answer_summary,
                    record.score,
                    json.dumps(record.strengths),
                    json.dumps(record.gaps),
                    record.coaching_note,
                    record.follow_up,
                    record.asked_at.isoformat(),
                    record.due_at.isoformat(),
                    record.repetition_count,
                    record.interval_days,
                    record.easiness,
                    record.related_record_id,
                ),
            )

    def get_recent_records(
        self,
        section: InterviewSection | None = None,
        limit: int = 20,
    ) -> list[InterviewRecord]:
        query = "SELECT * FROM interview_records"
        params: list[object] = []
        if section:
            query += " WHERE section = ?"
            params.append(section.value)
        query += " ORDER BY asked_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_due_records(
        self,
        section: InterviewSection,
        limit: int = 5,
        reference_time: datetime | None = None,
    ) -> list[InterviewRecord]:
        now = reference_time or _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM interview_records
                WHERE section = ? AND due_at <= ?
                ORDER BY score ASC, due_at ASC, asked_at ASC
                LIMIT ?
                """,
                (section.value, now.isoformat(), limit),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_record(self, record_id: str) -> InterviewRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM interview_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def has_recent_question_hash(self, question_hash: str, limit: int = 20) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM interview_records
                WHERE question_hash = ?
                ORDER BY asked_at DESC
                LIMIT ?
                """,
                (question_hash, limit),
            ).fetchone()
        return row is not None

    def best_previous_attempt(
        self,
        section: InterviewSection,
        topics: list[str],
    ) -> InterviewRecord | None:
        candidates = self.get_recent_records(section=section, limit=50)
        best_match: InterviewRecord | None = None
        best_overlap = 0
        topic_set = {topic.lower() for topic in topics}
        for candidate in candidates:
            overlap = len(topic_set.intersection({topic.lower() for topic in candidate.topics}))
            if overlap > best_overlap:
                best_match = candidate
                best_overlap = overlap
        return best_match

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS interview_records (
                    record_id TEXT PRIMARY KEY,
                    section TEXT NOT NULL,
                    question TEXT NOT NULL,
                    question_hash TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    topics_json TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    answer_summary TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    strengths_json TEXT NOT NULL,
                    gaps_json TEXT NOT NULL,
                    coaching_note TEXT NOT NULL,
                    follow_up TEXT,
                    asked_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    repetition_count INTEGER NOT NULL,
                    interval_days INTEGER NOT NULL,
                    easiness REAL NOT NULL,
                    related_record_id TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_section_due_at "
                "ON interview_records(section, due_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_question_hash "
                "ON interview_records(question_hash)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _row_to_record(self, row: sqlite3.Row) -> InterviewRecord:
        return InterviewRecord(
            record_id=str(row["record_id"]),
            section=InterviewSection(str(row["section"])),
            question=str(row["question"]),
            question_hash=str(row["question_hash"]),
            rationale=str(row["rationale"]),
            topics=json.loads(str(row["topics_json"])),
            difficulty=Difficulty(str(row["difficulty"])),
            source_inspiration=json.loads(str(row["source_refs_json"])),
            answer_text=str(row["answer_text"]),
            answer_summary=str(row["answer_summary"]),
            score=int(row["score"]),
            strengths=json.loads(str(row["strengths_json"])),
            gaps=json.loads(str(row["gaps_json"])),
            coaching_note=str(row["coaching_note"]),
            follow_up=row["follow_up"],
            asked_at=datetime.fromisoformat(str(row["asked_at"])),
            due_at=datetime.fromisoformat(str(row["due_at"])),
            repetition_count=int(row["repetition_count"]),
            interval_days=int(row["interval_days"]),
            easiness=float(row["easiness"]),
            related_record_id=row["related_record_id"],
        )
