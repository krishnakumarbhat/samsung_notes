from __future__ import annotations

import logging

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import InterviewRecord

logger = logging.getLogger(__name__)

try:
    from supabase import Client, create_client
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    Client = None
    create_client = None


class OptionalSupabaseSync:
    def __init__(self, settings: AppSettings) -> None:
        self._enabled = bool(
            settings.enable_supabase_sync
            and settings.supabase_url
            and settings.supabase_key
            and create_client
        )
        self._client: Client | None = None
        if self._enabled:
            self._client = create_client(settings.supabase_url, settings.supabase_key)

    def sync_record(self, record: InterviewRecord) -> bool:
        if not self._client:
            return False
        payload = {
            "record_id": record.record_id,
            "section": record.section.value,
            "question": record.question,
            "question_hash": record.question_hash,
            "rationale": record.rationale,
            "topics": record.topics,
            "difficulty": record.difficulty.value,
            "source_inspiration": record.source_inspiration,
            "answer_text": record.answer_text,
            "answer_summary": record.answer_summary,
            "score": record.score,
            "strengths": record.strengths,
            "gaps": record.gaps,
            "coaching_note": record.coaching_note,
            "follow_up": record.follow_up,
            "asked_at": record.asked_at.isoformat(),
            "due_at": record.due_at.isoformat(),
            "repetition_count": record.repetition_count,
            "interval_days": record.interval_days,
            "easiness": record.easiness,
            "related_record_id": record.related_record_id,
        }
        try:
            self._client.table("interview_records").upsert(payload).execute()
            return True
        except Exception:  # pragma: no cover - networked integration fallback.
            logger.exception("Supabase sync failed. Keeping data local only.")
            return False
