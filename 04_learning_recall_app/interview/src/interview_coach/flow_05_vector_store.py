from __future__ import annotations

import logging
from typing import Any, cast

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import InterviewRecord, InterviewSection, SemanticMemory
from interview_coach.flow_04_embeddings import embed_text

logger = logging.getLogger(__name__)

try:
    import chromadb
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    chromadb = None


class ChromaSemanticStore:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._enabled = chromadb is not None
        self._collection = None
        if self._enabled and chromadb is not None:
            client = chromadb.PersistentClient(path=str(settings.chroma_path))
            self._collection = client.get_or_create_collection(name="interview_memory")
        else:
            logger.warning("chromadb is not available; semantic recall is disabled.")

    def upsert_record(self, record: InterviewRecord) -> None:
        if not self._collection:
            return
        document = self._compose_document(record)
        self._collection.upsert(
            ids=[record.record_id],
            documents=[document],
            embeddings=[embed_text(document)],
            metadatas=[
                {
                    "record_id": record.record_id,
                    "section": record.section.value,
                    "question": record.question,
                    "answer_summary": record.answer_summary,
                    "score": record.score,
                    "asked_at": record.asked_at.isoformat(),
                }
            ],
        )

    def search(
        self,
        query: str,
        section: InterviewSection | None = None,
        limit: int = 3,
    ) -> list[SemanticMemory]:
        if not self._collection or not query.strip():
            return []
        where = cast(Any, {"section": section.value}) if section else None
        result = self._collection.query(
            query_embeddings=[embed_text(query)],
            n_results=limit,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        memories: list[SemanticMemory] = []
        documents_group = result.get("documents") or [[]]
        metadatas_group = result.get("metadatas") or [[]]
        distances_group = result.get("distances") or [[]]
        documents = documents_group[0]
        metadatas = metadatas_group[0]
        distances = distances_group[0]
        for document, metadata, distance in zip(documents, metadatas, distances, strict=False):
            memories.append(
                SemanticMemory(
                    record_id=str(metadata["record_id"]),
                    section=InterviewSection(str(metadata["section"])),
                    summary=document[:400],
                    question=str(metadata["question"]),
                    answer_summary=str(metadata["answer_summary"]),
                    distance=float(distance) if distance is not None else None,
                )
            )
        return memories

    def _compose_document(self, record: InterviewRecord) -> str:
        topics = ", ".join(record.topics)
        return (
            f"Section: {record.section.value}\n"
            f"Topics: {topics}\n"
            f"Question: {record.question}\n"
            f"Answer Summary: {record.answer_summary}\n"
            f"Coaching: {record.coaching_note}"
        )
