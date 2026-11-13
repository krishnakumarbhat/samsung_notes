from __future__ import annotations

import json
import logging
import re
from collections import Counter

from interview_coach.flow_00_config import AppSettings
from interview_coach.flow_01_models import (
    AnswerEvaluation,
    Difficulty,
    EvaluationContext,
    InterviewSection,
    QuestionCandidate,
    QuestionGenerationContext,
    ResumeProfile,
)
from interview_coach.flow_08_prompting import PromptBuilder

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    genai = None
    types = None


class GeminiInterviewClient:
    def __init__(self, settings: AppSettings, prompt_builder: PromptBuilder) -> None:
        self._settings = settings
        self._prompt_builder = prompt_builder
        self._client = None
        if genai and settings.gemini_api_key:
            self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_question(self, context: QuestionGenerationContext) -> QuestionCandidate:
        if not self._client or not types:
            return self.generate_local_question(context)

        prompt = self._prompt_builder.build_question_prompt(context)
        try:
            config = types.GenerateContentConfig(
                system_instruction=prompt.system_instruction,
                response_mime_type="application/json",
                temperature=0.35,
                thinking_config=types.ThinkingConfig(thinking_level="high"),
            )
            response = self._client.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt.user_prompt,
                config=config,
            )
            payload = self._extract_json(response.text)
            return QuestionCandidate(
                section=context.section,
                question=str(payload["question"]),
                rationale=str(payload.get("rationale", "Resume and source-grounded question.")),
                topics=[str(item) for item in payload.get("topics", [])][:8],
                difficulty=Difficulty(str(payload.get("difficulty", "intermediate"))),
                source_inspiration=[str(item) for item in payload.get("source_inspiration", [])][
                    :5
                ],
                active_recall_note=payload.get("active_recall_note"),
                review_reference_id=context.due_record.record_id if context.due_record else None,
            )
        except Exception:  # pragma: no cover - networked integration fallback.
            logger.exception("Gemini question generation failed. Falling back to local generation.")
            return self.generate_local_question(context)

    def evaluate_answer(self, context: EvaluationContext) -> AnswerEvaluation:
        if not self._client or not types:
            return self.evaluate_locally(context)

        prompt = self._prompt_builder.build_evaluation_prompt(context)
        try:
            config = types.GenerateContentConfig(
                system_instruction=prompt.system_instruction,
                response_mime_type="application/json",
                temperature=0.2,
                thinking_config=types.ThinkingConfig(thinking_level="high"),
            )
            response = self._client.models.generate_content(
                model=self._settings.gemini_model,
                contents=prompt.user_prompt,
                config=config,
            )
            payload = self._extract_json(response.text)
            return AnswerEvaluation(
                score=int(payload["score"]),
                strengths=[str(item) for item in payload.get("strengths", [])][:4],
                gaps=[str(item) for item in payload.get("gaps", [])][:4],
                coaching_note=str(
                    payload.get("coaching_note", "Add more depth and production detail.")
                ),
                follow_up=payload.get("follow_up"),
                should_repeat=bool(payload.get("should_repeat", False)),
                answer_summary=str(payload.get("answer_summary", "")),
            )
        except Exception:  # pragma: no cover - networked integration fallback.
            logger.exception("Gemini answer evaluation failed. Falling back to local scoring.")
            return self.evaluate_locally(context)

    def generate_local_question(self, context: QuestionGenerationContext) -> QuestionCandidate:
        topic = self._pick_topic(context.target_topics, context.resume_profile)
        recall_note = None
        if context.due_record:
            recall_note = (
                "We discussed a related area earlier. Start by recalling your previous answer, "
                "then extend it to this sharper scenario."
            )
        question = self._build_template_question(
            section=context.section,
            topic=topic,
            resume_profile=context.resume_profile,
            jd_text=context.jd_text,
            recall_note=recall_note,
        )
        return QuestionCandidate(
            section=context.section,
            question=question,
            rationale=(
                "Local fallback question generated from resume focus areas and source themes."
            ),
            topics=[topic],
            difficulty=(
                Difficulty.ADVANCED
                if context.section != InterviewSection.HR
                else Difficulty.INTERMEDIATE
            ),
            source_inspiration=context.source_notes[:3],
            active_recall_note=recall_note,
            review_reference_id=context.due_record.record_id if context.due_record else None,
        )

    def evaluate_locally(self, context: EvaluationContext) -> AnswerEvaluation:
        answer = context.answer_text.strip()
        word_count = len(answer.split())
        topic_hits = sum(1 for topic in context.candidate.topics if topic.lower() in answer.lower())
        keyword_hits = self._keyword_overlap(context.candidate.question, answer)
        score = 1
        if word_count > 40:
            score += 1
        if word_count > 110:
            score += 1
        if topic_hits:
            score += 1
        if keyword_hits >= 4:
            score += 1
        score = max(1, min(5, score))

        strengths: list[str] = []
        gaps: list[str] = []
        if word_count > 80:
            strengths.append(
                "You gave enough detail to evaluate reasoning instead of a one-line answer."
            )
        else:
            gaps.append("The answer is too short to show strong depth or trade-off thinking.")
        if keyword_hits >= 4:
            strengths.append("You referenced key technical concepts related to the question.")
        else:
            gaps.append("Core technical concepts from the question were only partially covered.")
        if topic_hits:
            strengths.append("You stayed aligned with the target topic rather than drifting away.")
        else:
            gaps.append("The response did not directly anchor itself to the requested topic.")

        should_repeat = score < self._settings.min_good_score
        coaching_note = (
            "Tighten the answer around failure modes, trade-offs, metrics, and debugging steps."
            if should_repeat
            else (
                "Good base answer. Push one level deeper into constraints, trade-offs, "
                "and measurement."
            )
        )
        follow_up = None
        if score >= 4:
            follow_up = (
                "What would break first if the traffic doubled and latency targets "
                "stayed unchanged?"
            )
        summary_words = answer.split()[:50]
        return AnswerEvaluation(
            score=score,
            strengths=strengths or ["The answer addresses the question directly."],
            gaps=gaps or ["Add more concrete examples from production work."],
            coaching_note=coaching_note,
            follow_up=follow_up,
            should_repeat=should_repeat,
            answer_summary=" ".join(summary_words),
        )

    def _build_template_question(
        self,
        section: InterviewSection,
        topic: str,
        resume_profile: ResumeProfile | None,
        jd_text: str | None,
        recall_note: str | None,
    ) -> str:
        recent_role = resume_profile.roles[0] if resume_profile and resume_profile.roles else None
        company = recent_role.company if recent_role else "your recent work"
        role = recent_role.title if recent_role else "your role"
        prefix = f"{recall_note} " if recall_note else ""

        if section == InterviewSection.RESUME:
            return (
                f"{prefix}In {role} at {company}, you worked around {topic}. "
                "Walk me through a concrete production failure mode in that area, "
                "the signals you would monitor, and the design "
                "changes you would make to prevent the same failure from recurring."
            )
        if section == InterviewSection.GITHUB:
            return (
                f"{prefix}Take {topic} as the focus. Explain how you would turn it into "
                "a strong interview-grade system design answer: architecture, trade-offs, "
                "failure cases, and what metrics would convince "
                "you the system is working."
            )
        if section == InterviewSection.DEEP_DIVE:
            jd_suffix = (
                ""
                if not jd_text
                else "Also align the answer to the uploaded job description where relevant. "
            )
            return (
                f"{prefix}Connect your hands-on experience with {topic} to a deeper "
                f"systems discussion. {jd_suffix}"
                "I want you to explain the design end-to-end, justify the major trade-offs, "
                "and identify the "
                "bottleneck you would expect to dominate first at scale."
            )
        if section == InterviewSection.CODING:
            return (
                f"{prefix}Coding round: design and explain an efficient solution for a "
                f"{topic}-style problem. "
                "You have a list of interview questions, each with a due day, difficulty, "
                "topic set, and recent history window. Return the next best question to ask "
                "without repeating a recently asked exact question, while prioritizing "
                "overdue weak areas. Discuss the data structures, time complexity, "
                "and edge cases before you code."
            )
        return (
            f"{prefix}Why are you a strong fit for a role centered on {topic}, and why "
            "would you consider changing company now? I want a grounded answer covering "
            "motivation, scope, compensation expectations, and "
            "how you evaluate whether the move is actually better for your long-term growth."
        )

    def _pick_topic(self, target_topics: list[str], resume_profile: ResumeProfile | None) -> str:
        if target_topics:
            return target_topics[0]
        if resume_profile and resume_profile.focus_areas:
            return resume_profile.focus_areas[0]
        return "production ML systems"

    def _extract_json(self, response_text: str) -> dict[str, object]:
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", response_text, flags=re.DOTALL)
            if not match:
                raise
            return json.loads(match.group(0))

    def _keyword_overlap(self, question: str, answer: str) -> int:
        question_tokens = [
            token for token in re.findall(r"[a-zA-Z0-9_+.:-]+", question.lower()) if len(token) > 3
        ]
        answer_tokens = Counter(re.findall(r"[a-zA-Z0-9_+.:-]+", answer.lower()))
        overlap = 0
        for token in question_tokens[:12]:
            if answer_tokens[token]:
                overlap += 1
        return overlap
