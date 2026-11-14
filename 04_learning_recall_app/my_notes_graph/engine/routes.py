"""HTTP surface for the question bank, flow scheduler and mobile sync."""
from __future__ import annotations

import json
import threading
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import auth, elo, flow_scheduler, games, interleaving, network, quiz_factory, sync_bridge
from . import background_question_factory as background_factory

router = APIRouter()

_GENERATION_LOCK = threading.Lock()
_GENERATION: dict = {"running": False, "last": None}


class ReviewItem(BaseModel):
    """One completed question as reported by a device."""

    quiz_id: int = Field(ge=1)
    user_selection: int | None = Field(default=None, ge=0, le=3)
    time_spent_seconds: int = Field(default=0, ge=0, le=86400)
    flow_rating: int = Field(ge=1, le=5)
    context_mode: Literal["DESK_STANDARD", "COMMUTE_TRAVEL"] = "DESK_STANDARD"
    error_category: Literal["ALGEBRAIC_SLIP", "CONCEPTUAL_GAP", "TRAP_OVERLOOKED", "NONE"] | None = None
    client_id: str | None = Field(default=None, max_length=120)
    reviewed_at: str | None = None


class PushReviewsRequest(BaseModel):
    """A batch upload; bounded so a single request cannot stall the server."""

    reviews: list[ReviewItem] = Field(min_length=1, max_length=200)
    session_id: str = Field(default="default", max_length=80)


class PostponeRequest(BaseModel):
    """Defer one question until the learner is back at a desk."""

    quiz_id: int = Field(ge=1)
    hours: float = Field(default=24.0, gt=0, le=24 * 30)


class FlowRequest(BaseModel):
    """Session momentum update after one answered question."""

    flow_rating: int = Field(ge=1, le=5)
    session_id: str = Field(default="default", max_length=80)
    context_mode: Literal["DESK_STANDARD", "COMMUTE_TRAVEL"] = "DESK_STANDARD"


class GenerateRequest(BaseModel):
    """Ask the factory for a bounded batch of new questions."""

    count: int = Field(default=25, ge=1, le=500)
    per_batch: int = Field(default=5, ge=4, le=6)


class WeaknessRequest(BaseModel):
    """A missed question that should return as a variant later in the session."""

    quiz_id: int = Field(ge=1)
    session_id: str = Field(default="default", max_length=80)
    position: int = Field(default=0, ge=0, le=1000)
    correct: bool | None = None


class GameScoreRequest(BaseModel):
    """One finished game round; an offline retry reuses ``client_id``."""

    game: Literal["sudoku", "trap_hunt", "match"]
    topic: str = Field(default="", max_length=200)
    score: int = Field(default=0, ge=0, le=1_000_000)
    accuracy: float = Field(default=0.0, ge=0.0, le=1.0)
    duration_seconds: int = Field(default=0, ge=0, le=86400)
    detail: dict | None = None
    client_id: str | None = Field(default=None, max_length=120)


@router.get("/api/quiz/stats")
def quiz_stats() -> dict:
    """Bank, review and travel-readiness counters for the sync modal."""
    return flow_scheduler.stats()


@router.get("/api/quiz/queue")
def quiz_queue(limit: int = Query(default=25, ge=1, le=200),
               mode: Literal["all", "travel"] = "all",
               session_id: str = "default",
               shuffle: bool = True,
               recycle: bool = True,
               seed: int | None = Query(default=None, ge=0, le=2**32 - 1)) -> dict:
    """Interleaved queue for the current session and context mode.

    The order is an Elo-matched seeded sample by default; ``shuffle=0`` returns
    the plain urgency order and ``seed`` reproduces a specific sequence.
    """
    chosen = seed
    if shuffle and chosen is None:
        chosen = flow_scheduler.daily_seed(session_id)
    return flow_scheduler.build_queue(limit=limit, mode=mode, session_id=session_id,
                                      seed=chosen if shuffle else None, recycle=recycle)


@router.get("/api/elo")
def elo_snapshot() -> dict:
    """Learner ratings with decay already applied, per domain and overall."""
    return elo.snapshot()


@router.get("/api/sync/state")
def sync_state() -> dict:
    """Answered/postponed ids and ratings so a device can reconcile its cache."""
    return sync_bridge.state()


@router.get("/api/sync/pull_deck")
def pull_deck(limit: int = Query(default=100, ge=1, le=500),
              mode: Literal["all", "travel"] = "all",
              include_synced: bool = False) -> dict:
    """Bundle staged questions for a device and mark them as synced."""
    return sync_bridge.pull_deck(limit=limit, mode=mode, include_synced=include_synced)


@router.get("/api/sync/export_deck")
def export_deck(limit: int = Query(default=100, ge=1, le=500),
                mode: Literal["all", "travel"] = "all",
                download: bool = True) -> Response:
    """Download the same deck as a file without marking it synced."""
    payload = sync_bridge.deck_for_download(limit=limit, mode=mode)
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    headers = {}
    if download:
        stamp = payload["generated_at"][:10]
        headers["Content-Disposition"] = f'attachment; filename="recall-deck-{stamp}.json"'
    return Response(body, media_type="application/json", headers=headers)


@router.get("/api/sync/pull_batch")
def pull_batch(count: int = Query(default=100, ge=1, le=300),
               mode: Literal["all", "travel"] = "all") -> dict:
    """Compact LAN batch (<=1.5 MB) for a phone on the same hotspot."""
    return sync_bridge.pull_batch(count=count, mode=mode)


@router.get("/api/network/info")
def network_info(request: Request) -> dict:
    """LAN URLs, mDNS state and the install note shown in the sync modal.

    The pairing code is only disclosed to the laptop itself (loopback); a
    paired phone sees the URLs but can never read the code back.
    """
    info = network.network_info()
    info["auth_required"] = auth.enabled()
    if auth.enabled() and auth.trusted(request.scope):
        code = auth.token()
        info["pairing_code"] = code
        info["pair_urls"] = [f"{url}/pair?token={code}" for url in info["urls"]]
    return info


@router.post("/api/sync/push_reviews")
def push_reviews(body: PushReviewsRequest) -> dict:
    """Accept device reviews, update FSRS state, and advance momentum."""
    reviews = [item.model_dump() for item in body.reviews]
    return sync_bridge.push_reviews(reviews, session_id=body.session_id)


@router.post("/api/quiz/postpone")
def postpone(body: PostponeRequest) -> dict:
    """Postpone a question by 24 hours without recording an FSRS lapse."""
    try:
        return flow_scheduler.postpone_quiz(body.quiz_id, hours=body.hours)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/api/quiz/flow")
def flow(body: FlowRequest) -> dict:
    """Record the five-point rating and return the adjusted target tier."""
    return flow_scheduler.record_rating(body.session_id, body.flow_rating,
                                        context_mode=body.context_mode)


@router.post("/api/quiz/weakness")
def weakness(body: WeaknessRequest) -> dict:
    """Schedule the variant, prerequisite or intuition card that follows a miss."""
    try:
        return interleaving.register_weakness(
            body.session_id, body.quiz_id, position=body.position, correct=body.correct)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


def _generate_in_background(count: int, per_batch: int) -> None:
    """Add ``count`` questions to the existing pool, releasing the lock always."""
    try:
        job = quiz_factory.QuizFactory()
        # The CLI target is a pool size; the modal button is additive.
        report = job.run(job.bank_size() + count, per_batch=per_batch, verbose=False)
        with _GENERATION_LOCK:
            _GENERATION["last"] = report.to_dict()
    except Exception as error:  # keep the server alive; the modal shows the message
        with _GENERATION_LOCK:
            _GENERATION["last"] = {"error": f"{type(error).__name__}: {error}"}
    finally:
        with _GENERATION_LOCK:
            _GENERATION["running"] = False


@router.post("/api/quiz/generate")
def generate(body: GenerateRequest, background: BackgroundTasks) -> dict:
    """Queue a background generation job; only one may run at a time."""
    with _GENERATION_LOCK:
        if _GENERATION["running"]:
            raise HTTPException(409, "a generation job is already running")
        _GENERATION["running"] = True
        _GENERATION["last"] = None
    background.add_task(_generate_in_background, body.count, body.per_batch)
    return {"status": "started", "target": body.count}


@router.get("/api/quiz/generate/status")
def generate_status() -> dict:
    """Poll the single background generation job."""
    with _GENERATION_LOCK:
        return {"running": bool(_GENERATION["running"]), "last": _GENERATION["last"]}


@router.get("/api/quiz/factory/status")
def factory_status() -> dict:
    """Offline-safe view of the opt-in background pre-generator."""
    status = background_factory.read_status()
    status["staged_now"] = background_factory.staged_count()
    return status


# --------------------------------------------------------------------- games
@router.get("/api/games/topics")
def game_topics() -> dict:
    """Topic folders with the nine concepts that personalise a board."""
    return {"topics": games.topics()}


@router.get("/api/games/sudoku")
def game_sudoku(topic: str = Query(default="", max_length=200),
                difficulty: Literal["easy", "medium", "hard"] = "medium",
                seed: int | None = Query(default=None, ge=0, le=2**31 - 1)) -> dict:
    """A uniquely solvable board plus the topic legend for symbol mode."""
    return games.sudoku(topic, difficulty=difficulty, seed=seed)


@router.get("/api/games/trap_hunt")
def game_trap_hunt(topic: str = Query(default="", max_length=200),
                   limit: int = Query(default=12, ge=1, le=40),
                   seed: int | None = Query(default=None, ge=0, le=2**31 - 1)) -> dict:
    """Desk-free adversarial questions for a timed combo round."""
    return games.trap_hunt(topic, limit=limit, seed=seed)


@router.get("/api/games/match")
def game_match(topic: str = Query(default="", max_length=200),
               pairs: int = Query(default=6, ge=3, le=12),
               seed: int | None = Query(default=None, ge=0, le=2**31 - 1)) -> dict:
    """Concept/clue cards for the flip-and-match board."""
    return games.match_pairs(topic, pairs=pairs, seed=seed)


@router.get("/api/games/scores")
def game_scores(topic: str = Query(default="", max_length=200)) -> dict:
    """Personal bests per game and the most recent rounds."""
    return games.scores(topic=topic)


@router.post("/api/games/score")
def game_score(body: GameScoreRequest) -> dict:
    """Persist a round (idempotent on ``client_id``) and return the best."""
    try:
        return games.record_score(
            body.game, topic=body.topic, score=body.score, accuracy=body.accuracy,
            duration_seconds=body.duration_seconds, detail=body.detail,
            client_id=body.client_id,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
