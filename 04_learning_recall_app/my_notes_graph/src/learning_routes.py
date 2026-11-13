"""Loopback-only study API routes for the three learning modes."""
from fastapi import APIRouter, HTTPException
from src import learning
from src.gemini_client import GeminiError
from src.study_request import StudyRequest

router = APIRouter()


def call(function, *args):
    """Expose actionable sanitized provider/input failures, not tracebacks."""
    try:
        return function(*args)
    except GeminiError as error:
        raise HTTPException(503, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/api/studio")
def studio(body: StudyRequest) -> dict:
    """Grounded synthesis/chat with source selections."""
    return call(learning.studio, body.action, body.question, body.sources)


@router.post("/api/exam/start")
def start(body: StudyRequest) -> dict:
    """Create a server-held exam, returning no solutions."""
    return call(learning.exam_start, body.sources, body.mode, body.count, body.duration_minutes)


@router.post("/api/exam/answer")
def answer(body: StudyRequest) -> dict:
    """Save a choice, revealing feedback only in practice mode."""
    return call(learning.exam_answer, body.exam_id, body.question_id, body.choice, body.marked)


@router.post("/api/exam/finish")
def finish(body: StudyRequest) -> dict:
    """Finish once and return complete score/weakness analytics."""
    return call(learning.exam_finish, body.exam_id)


@router.post("/api/tutor/step")
def tutor(body: StudyRequest) -> dict:
    """One concept plus one verification question per tutor turn."""
    return call(learning.tutor_step, body.session_id, body.sources, body.message)


@router.get("/api/learning/history")
def history() -> dict:
    """Expandable rich sessions with safe unfinished-exam redaction."""
    return learning.rich_history()


@router.get("/api/intelligence")
def intelligence() -> dict:
    """Thirty-day observed strengths, weaknesses, and next study suggestions."""
    return learning.intelligence()
