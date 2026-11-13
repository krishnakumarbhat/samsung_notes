from __future__ import annotations

import json

from pydantic import BaseModel

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import EvaluationContext, QuestionGenerationContext


class PromptPackage(BaseModel):
    system_instruction: str
    user_prompt: str


class PromptBuilder:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def build_question_prompt(self, context: QuestionGenerationContext) -> PromptPackage:
        resume_summary = context.resume_profile.compact_summary() if context.resume_profile else ""
        due_context = ""
        if context.due_record:
            due_context = (
                "Review this weak or due area with active recall. Previous question: "
                f"{context.due_record.question}. "
                f"Candidate summary: {context.due_record.answer_summary}."
            )
        prior_memories = [
            {
                "question": memory.question,
                "answer_summary": memory.answer_summary,
            }
            for memory in context.semantic_memories
        ]
        prompt = {
            "candidate_experience_years": self._settings.candidate_experience_years,
            "section": context.section.value,
            "resume_summary": resume_summary,
            "target_topics": context.target_topics,
            "source_notes": context.source_notes,
            "recent_questions": context.recent_questions,
            "prior_memories": prior_memories,
            "due_context": due_context,
            "job_description": context.jd_text or "",
            "response_format": {
                "question": "string",
                "rationale": "string",
                "topics": ["string"],
                "difficulty": "foundational|intermediate|advanced",
                "source_inspiration": ["string"],
                "active_recall_note": "string|null",
            },
            "constraints": [
                "Ask exactly one interview question.",
                "Make it advanced but suitable for roughly 3 years of experience.",
                "Do not repeat any recent question verbatim.",
                "If due_context exists, use active recall and reference the prior attempt briefly.",
                "Prefer practical production trade-offs over textbook recitation.",
                "Return valid JSON only.",
            ],
        }
        return PromptPackage(
            system_instruction=self._settings.interviewer_persona,
            user_prompt=json.dumps(prompt, indent=2),
        )

    def build_evaluation_prompt(self, context: EvaluationContext) -> PromptPackage:
        resume_summary = context.resume_profile.compact_summary() if context.resume_profile else ""
        prior_attempt = None
        if context.previous_attempt:
            prior_attempt = {
                "question": context.previous_attempt.question,
                "answer_summary": context.previous_attempt.answer_summary,
                "score": context.previous_attempt.score,
            }
        prompt = {
            "question": context.candidate.question,
            "topics": context.candidate.topics,
            "difficulty": context.candidate.difficulty.value,
            "answer": context.answer_text,
            "resume_summary": resume_summary,
            "job_description": context.jd_text or "",
            "prior_attempt": prior_attempt,
            "semantic_memories": [
                {
                    "question": memory.question,
                    "answer_summary": memory.answer_summary,
                }
                for memory in context.semantic_memories
            ],
            "response_format": {
                "score": "integer 1-5",
                "strengths": ["string"],
                "gaps": ["string"],
                "coaching_note": "string",
                "follow_up": "string|null",
                "should_repeat": "boolean",
                "answer_summary": "string",
            },
            "constraints": [
                "Evaluate only the candidate answer quality, not the resume prestige.",
                "Reward clear reasoning, trade-offs, and production debugging detail.",
                "Penalize vague claims and missing constraints.",
                "Return valid JSON only.",
            ],
        }
        return PromptPackage(
            system_instruction=(
                "You are a strict but fair interviewer. Score the answer on a 1-5 scale. "
                "A score of 4 or 5 means the exact question should not be repeated soon; weaker "
                "answers should be scheduled again with active recall."
            ),
            user_prompt=json.dumps(prompt, indent=2),
        )
