from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import (
    EvaluationContext,
    HistoryView,
    InterviewRecord,
    InterviewSection,
    QuestionCandidate,
    QuestionGenerationContext,
    ResumeProfile,
)
from interview_coach.flow_02_resume import ResumeParser
from interview_coach.flow_03_sources import SourceCatalogService
from interview_coach.flow_05_vector_store import ChromaSemanticStore
from interview_coach.flow_06_sqlite_store import SQLiteInterviewStore
from interview_coach.flow_07_scheduler import SpacedRepetitionScheduler
from interview_coach.flow_08_prompting import PromptBuilder
from interview_coach.flow_09_llm import GeminiInterviewClient
from interview_coach.flow_11_supabase import OptionalSupabaseSync

logger = logging.getLogger(__name__)


class InterviewService:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings.from_env()
        self.settings.ensure_directories()
        logging.basicConfig(level=logging.INFO)

        self._resume_parser = ResumeParser()
        self._catalog = SourceCatalogService()
        self._sqlite_store = SQLiteInterviewStore(self.settings)
        self._vector_store = ChromaSemanticStore(self.settings)
        self._scheduler = SpacedRepetitionScheduler()
        self._prompt_builder = PromptBuilder(self.settings)
        self._llm = GeminiInterviewClient(self.settings, self._prompt_builder)
        self._supabase_sync = OptionalSupabaseSync(self.settings)

    def load_resume_profile(
        self,
        manual_text: str | None = None,
        pdf_path: Path | None = None,
    ) -> ResumeProfile:
        target_path = pdf_path or self.settings.default_resume_path
        return self._resume_parser.extract_profile(pdf_path=target_path, manual_text=manual_text)

    def extract_temporary_text(self, filename: str, content: bytes) -> str:
        return self._resume_parser.extract_temporary_text(filename=filename, content=content)

    def generate_question(
        self,
        section: InterviewSection,
        resume_profile: ResumeProfile,
        jd_text: str | None = None,
    ) -> QuestionCandidate:
        due_record = next(iter(self._sqlite_store.get_due_records(section=section, limit=1)), None)
        target_topics = self._build_target_topics(section, resume_profile, due_record)
        source_notes = self._catalog.source_notes_for_section(section, resume_profile)
        recent_records = self._sqlite_store.get_recent_records(
            section=section,
            limit=self.settings.recent_question_window,
        )
        semantic_memories = self._vector_store.search(
            query=" ".join(target_topics),
            section=section,
            limit=self.settings.semantic_results_limit,
        )
        candidate = self._llm.generate_question(
            QuestionGenerationContext(
                section=section,
                resume_profile=resume_profile,
                target_topics=target_topics,
                source_notes=source_notes,
                recent_questions=[record.question for record in recent_records],
                semantic_memories=semantic_memories,
                due_record=due_record,
                jd_text=jd_text,
            )
        )
        if self._is_recent_duplicate(candidate.question, recent_records):
            logger.info("Detected duplicate question. Regenerating using local distinct template.")
            candidate = self._llm.generate_local_question(
                QuestionGenerationContext(
                    section=section,
                    resume_profile=resume_profile,
                    target_topics=target_topics[1:] + target_topics[:1],
                    source_notes=source_notes,
                    recent_questions=[record.question for record in recent_records],
                    semantic_memories=semantic_memories,
                    due_record=due_record,
                    jd_text=jd_text,
                )
            )
        return candidate

    def evaluate_answer(
        self,
        candidate: QuestionCandidate,
        answer_text: str,
        resume_profile: ResumeProfile,
        jd_text: str | None = None,
    ) -> InterviewRecord:
        if candidate.review_reference_id:
            previous_attempt = self._sqlite_store.get_record(candidate.review_reference_id)
        else:
            previous_attempt = self._sqlite_store.best_previous_attempt(
                section=candidate.section,
                topics=candidate.topics,
            )
        semantic_memories = self._vector_store.search(
            query=" ".join(candidate.topics),
            section=candidate.section,
            limit=self.settings.semantic_results_limit,
        )
        evaluation = self._llm.evaluate_answer(
            EvaluationContext(
                candidate=candidate,
                answer_text=answer_text,
                resume_profile=resume_profile,
                jd_text=jd_text,
                previous_attempt=previous_attempt,
                semantic_memories=semantic_memories,
            )
        )
        review_plan = self._scheduler.next_review(
            score=evaluation.score,
            previous_record=previous_attempt,
        )
        question_hash = self.hash_text(candidate.question)
        record = InterviewRecord(
            section=candidate.section,
            question=candidate.question,
            question_hash=question_hash,
            rationale=candidate.rationale,
            topics=candidate.topics,
            difficulty=candidate.difficulty,
            source_inspiration=candidate.source_inspiration,
            answer_text=answer_text.strip(),
            answer_summary=evaluation.answer_summary,
            score=evaluation.score,
            strengths=evaluation.strengths,
            gaps=evaluation.gaps,
            coaching_note=evaluation.coaching_note,
            follow_up=evaluation.follow_up,
            asked_at=datetime.now(timezone.utc),
            due_at=review_plan.due_at,
            repetition_count=review_plan.repetition_count,
            interval_days=review_plan.interval_days,
            easiness=review_plan.easiness,
            related_record_id=previous_attempt.record_id if previous_attempt else None,
        )
        self._sqlite_store.save_record(record)
        self._vector_store.upsert_record(record)
        self._supabase_sync.sync_record(record)
        return record

    def recent_history(self, section: InterviewSection, limit: int = 8) -> list[HistoryView]:
        return [
            HistoryView(
                question=record.question,
                score=record.score,
                due_at=record.due_at,
                topics=record.topics,
            )
            for record in self._sqlite_store.get_recent_records(section=section, limit=limit)
        ]

    def due_count(self, section: InterviewSection) -> int:
        return len(self._sqlite_store.get_due_records(section=section, limit=50))

    def _build_target_topics(
        self,
        section: InterviewSection,
        resume_profile: ResumeProfile,
        due_record: InterviewRecord | None,
    ) -> list[str]:
        if due_record:
            return due_record.topics or resume_profile.focus_areas[:5]
        if section == InterviewSection.CODING:
            return self._catalog.coding_topics(resume_profile)
        if section == InterviewSection.HR:
            hr_topics = [
                "career transitions",
                "project impact",
                "scope and ownership",
                "salary negotiation",
                "team collaboration",
            ]
            return hr_topics
        catalog_topics = self._catalog.topics_for_section(section, resume_profile)
        if section == InterviewSection.DEEP_DIVE:
            return [*resume_profile.focus_areas[:5], *catalog_topics][:10]
        if section == InterviewSection.GITHUB:
            return catalog_topics[:10]
        return [*resume_profile.focus_areas[:8], *catalog_topics[:4]][:10]

    def _is_recent_duplicate(
        self,
        question: str,
        recent_records: list[InterviewRecord],
    ) -> bool:
        normalized_question = self._normalize_question(question)
        if self._sqlite_store.has_recent_question_hash(self.hash_text(question)):
            return True
        for record in recent_records:
            normalized_existing = self._normalize_question(record.question)
            if normalized_question == normalized_existing:
                return True
            overlap = self._token_overlap(normalized_question, normalized_existing)
            if overlap >= 0.85:
                return True
        return False

    def _normalize_question(self, question: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", question.lower())).strip()

    def _token_overlap(self, first: str, second: str) -> float:
        first_tokens = set(first.split())
        second_tokens = set(second.split())
        if not first_tokens or not second_tokens:
            return 0.0
        intersection = len(first_tokens.intersection(second_tokens))
        union = len(first_tokens.union(second_tokens))
        return intersection / union

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
