from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class InterviewSection(str, Enum):
    RESUME = "resume"
    GITHUB = "github"
    DEEP_DIVE = "deep_dive"
    CODING = "coding"
    HR = "hr"

    @property
    def label(self) -> str:
        labels = {
            InterviewSection.RESUME: "Resume",
            InterviewSection.GITHUB: "GitHub",
            InterviewSection.DEEP_DIVE: "Deep Dive",
            InterviewSection.CODING: "Coding",
            InterviewSection.HR: "HR",
        }
        return labels[self]


class Difficulty(str, Enum):
    FOUNDATIONAL = "foundational"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class RoleExperience(BaseModel):
    company: str
    title: str
    period: str = ""
    summary_bullets: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ProjectSummary(BaseModel):
    name: str
    descriptor: str = ""
    summary_bullets: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ResumeProfile(BaseModel):
    raw_text: str = ""
    roles: list[RoleExperience] = Field(default_factory=list)
    projects: list[ProjectSummary] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    focus_areas: list[str] = Field(default_factory=list)

    def compact_summary(self) -> str:
        role_bits = [f"{role.title} at {role.company}" for role in self.roles[:2]]
        project_bits = [project.name for project in self.projects[:2]]
        focus_bits = ", ".join(self.focus_areas[:8])
        segments = []
        if role_bits:
            segments.append("Recent roles: " + "; ".join(role_bits))
        if project_bits:
            segments.append("Projects: " + "; ".join(project_bits))
        if focus_bits:
            segments.append("Focus: " + focus_bits)
        return " | ".join(segments)


class SourceTopic(BaseModel):
    name: str
    url: str
    categories: list[str] = Field(default_factory=list)
    interview_angles: list[str] = Field(default_factory=list)


class SemanticMemory(BaseModel):
    record_id: str
    section: InterviewSection
    summary: str
    question: str
    answer_summary: str
    distance: float | None = None


class QuestionCandidate(BaseModel):
    candidate_id: str = Field(default_factory=lambda: str(uuid4()))
    section: InterviewSection
    question: str
    rationale: str
    topics: list[str] = Field(default_factory=list)
    difficulty: Difficulty = Difficulty.INTERMEDIATE
    source_inspiration: list[str] = Field(default_factory=list)
    active_recall_note: str | None = None
    review_reference_id: str | None = None


class AnswerEvaluation(BaseModel):
    score: int = Field(ge=1, le=5)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    coaching_note: str
    follow_up: str | None = None
    should_repeat: bool
    answer_summary: str


class ReviewPlan(BaseModel):
    repetition_count: int
    interval_days: int
    easiness: float
    due_at: datetime


class InterviewRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    record_id: str = Field(default_factory=lambda: str(uuid4()))
    section: InterviewSection
    question: str
    question_hash: str
    rationale: str
    topics: list[str] = Field(default_factory=list)
    difficulty: Difficulty
    source_inspiration: list[str] = Field(default_factory=list)
    answer_text: str
    answer_summary: str
    score: int
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    coaching_note: str
    follow_up: str | None = None
    asked_at: datetime
    due_at: datetime
    repetition_count: int = 0
    interval_days: int = 0
    easiness: float = 2.5
    related_record_id: str | None = None


class QuestionGenerationContext(BaseModel):
    section: InterviewSection
    resume_profile: ResumeProfile | None = None
    target_topics: list[str] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)
    recent_questions: list[str] = Field(default_factory=list)
    semantic_memories: list[SemanticMemory] = Field(default_factory=list)
    due_record: InterviewRecord | None = None
    jd_text: str | None = None


class EvaluationContext(BaseModel):
    candidate: QuestionCandidate
    answer_text: str
    resume_profile: ResumeProfile | None = None
    jd_text: str | None = None
    previous_attempt: InterviewRecord | None = None
    semantic_memories: list[SemanticMemory] = Field(default_factory=list)


class HistoryView(BaseModel):
    question: str
    score: int
    due_at: datetime
    topics: list[str] = Field(default_factory=list)


class JsonPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    payload: dict[str, Any]
