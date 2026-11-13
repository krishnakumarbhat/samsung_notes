from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    app_name: str = "Interview Memory Coach"
    storage_dir: Path = Field(default_factory=lambda: Path("storage"))
    default_resume_path: Path = Field(default_factory=lambda: Path("krishna_resume.pdf"))
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    gemini_api_key: str | None = None
    enable_supabase_sync: bool = False
    supabase_url: str | None = None
    supabase_key: str | None = None
    candidate_experience_years: int = 3
    min_good_score: int = 4
    recent_question_window: int = 20
    semantic_results_limit: int = 3
    interviewer_persona: str = (
        "You are a principal-level AI/ML interviewer with 20 years of experience in "
        "machine learning systems, large-scale software engineering, and hiring. Ask one "
        "question at a time. Keep the tone professional, precise, and demanding without "
        "being overwhelming. Calibrate difficulty for a candidate with 3 years of "
        "experience. Prefer resume-grounded questions first, then deepen into system "
        "trade-offs, debugging, production design, and coding. Avoid repeating exact "
        "questions. When revisiting a weak area, acknowledge prior discussion briefly and "
        "ask a related active-recall follow-up."
    )

    @classmethod
    def from_env(cls) -> AppSettings:
        storage_dir = Path(os.getenv("INTERVIEW_STORAGE_DIR", "storage"))
        return cls(
            storage_dir=storage_dir,
            default_resume_path=Path(os.getenv("DEFAULT_RESUME_PATH", "krishna_resume.pdf")),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            enable_supabase_sync=os.getenv("ENABLE_SUPABASE_SYNC", "false").lower()
            in {"1", "true", "yes", "on"},
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_key=os.getenv("SUPABASE_KEY"),
        )

    @property
    def sqlite_path(self) -> Path:
        return self.storage_dir / "interview_memory.sqlite3"

    @property
    def chroma_path(self) -> Path:
        return self.storage_dir / "chroma"

    def ensure_directories(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)
