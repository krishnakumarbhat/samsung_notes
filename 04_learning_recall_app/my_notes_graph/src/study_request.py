"""Validated small JSON requests for the local learning workstation."""
from pydantic import BaseModel, Field


class StudyRequest(BaseModel):
    """Bound all user-controlled text, selection sizes and exam parameters."""

    sources: list[str] = Field(default_factory=list, max_length=274)
    action: str = Field(default="chat", max_length=20)
    question: str = Field(default="", max_length=1000)
    message: str = Field(default="", max_length=1000)
    session_id: str | None = Field(default=None, max_length=40)
    exam_id: str = Field(default="", max_length=40)
    question_id: int = Field(default=1, ge=1, le=25)
    choice: int | None = Field(default=None, ge=0, le=3)
    marked: bool = False
    mode: str = Field(default="practice", pattern="^(practice|exam)$")
    count: int = Field(default=25, ge=1, le=25)
    duration_minutes: int = Field(default=45, ge=1, le=180)
