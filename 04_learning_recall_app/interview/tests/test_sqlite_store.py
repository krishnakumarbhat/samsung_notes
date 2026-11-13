from datetime import datetime, timedelta, timezone

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import Difficulty, InterviewRecord, InterviewSection
from interview_coach.flow_06_sqlite_store import SQLiteInterviewStore


def test_store_saves_and_reads_due_records(tmp_path) -> None:
    settings = AppSettings(storage_dir=tmp_path / "storage")
    settings.ensure_directories()
    store = SQLiteInterviewStore(settings)
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)

    record = InterviewRecord(
        section=InterviewSection.RESUME,
        question="Explain your RAG pipeline design.",
        question_hash="hash-1",
        rationale="Test rationale",
        topics=["RAG"],
        difficulty=Difficulty.ADVANCED,
        source_inspiration=["source"],
        answer_text="I would start with retrieval quality.",
        answer_summary="Starts with retrieval quality.",
        score=3,
        strengths=["Clear start"],
        gaps=["Needs metrics"],
        coaching_note="Add metrics.",
        follow_up=None,
        asked_at=now,
        due_at=now - timedelta(days=1),
        repetition_count=1,
        interval_days=2,
        easiness=2.4,
    )
    store.save_record(record)

    due_records = store.get_due_records(InterviewSection.RESUME, limit=5, reference_time=now)

    assert len(due_records) == 1
    assert due_records[0].question == record.question
