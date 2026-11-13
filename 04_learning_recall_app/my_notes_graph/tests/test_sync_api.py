"""HTTP sync protocol: deck pull, review upload, FSRS updates and validation."""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.routes import router
from engine.sync_bridge import MAX_DECK_BYTES
from tests.bank_fixtures import (
    add_card, add_question, bulk_questions, make_db, sqlite_rows,
)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """A bank-backed router with its own database and no host application."""
    path = make_db(tmp_path)
    add_card(path)
    monkeypatch.setenv("RECALL_DB_PATH", str(path))
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as client:
        yield client, path


def review_body(quiz_id: int, **overrides) -> dict:
    """A valid device review, overridable per test."""
    body = {
        "quiz_id": quiz_id,
        "user_selection": 1,
        "time_spent_seconds": 42,
        "flow_rating": 2,
        "context_mode": "DESK_STANDARD",
        "error_category": "NONE",
        "client_id": "device-1",
        "reviewed_at": "2026-10-28T09:30:00+00:00",
    }
    body.update(overrides)
    if "quiz_id" in overrides:            # avoid a duplicate keyword argument
        body["quiz_id"] = overrides["quiz_id"]
    return body


# --------------------------------------------------------------------- stats
def test_stats_endpoint_returns_the_modal_counters(api):
    """The sync modal reads a single grouped snapshot."""
    client, path = api
    add_question(path)
    response = client.get("/api/quiz/stats")
    assert response.status_code == 200
    payload = response.json()
    for key in ("bank_total", "available", "synced_to_device", "completed",
                "postponed", "reviews_logged", "travel_ready", "by_type"):
        assert key in payload
    assert payload["available"] == 1


# ---------------------------------------------------------------- pull a deck
def test_pull_deck_marks_questions_synced_and_returns_a_self_contained_deck(api):
    """A deck carries options, answers and explanations, then is marked synced."""
    client, path = api
    bulk_questions(path, 6)
    response = client.get("/api/sync/pull_deck", params={"limit": 100})
    assert response.status_code == 200
    deck = response.json()
    assert deck["count"] == 6 and deck["mode"] == "all"
    assert deck["bytes"] < MAX_DECK_BYTES
    question = deck["questions"][0]
    assert {"id", "question_md", "options", "correct_index", "explanation_md",
            "cognitive_type", "requires_scratchpad"} <= set(question)
    assert len(question["options"]) == 4
    statuses = sqlite_rows(path, "SELECT status FROM quiz_bank")
    assert all(row["status"] == "SYNCED_TO_MOBILE" for row in statuses)


def test_pull_deck_travel_mode_never_includes_a_scratchpad_question(api):
    """Travel mode filters on the flag, and postpones the paper questions."""
    client, path = api
    mental = add_question(path, cognitive="INTUITION", text="Mental $1+1$?")
    paper = add_question(path, cognitive="CALCULATION_STEP", text="Paper $\\int x$?")
    deck = client.get("/api/sync/pull_deck", params={"limit": 100, "mode": "travel"}).json()
    ids = {question["id"] for question in deck["questions"]}
    assert ids == {mental}
    assert paper not in ids
    statuses = {row["id"]: row["status"] for row in
                sqlite_rows(path, "SELECT id,status FROM quiz_bank")}
    assert statuses[paper] == "POSTPONED"
    assert statuses[mental] == "SYNCED_TO_MOBILE"
    assert deck["postponed"] == 1


def test_pull_deck_can_rebuild_a_deck_already_on_the_device(api):
    """Clearing phone storage must not require answering the whole deck first."""
    client, path = api
    add_question(path)
    first = client.get("/api/sync/pull_deck", params={"limit": 10}).json()
    empty = client.get("/api/sync/pull_deck", params={"limit": 10}).json()
    rebuilt = client.get("/api/sync/pull_deck",
                         params={"limit": 10, "include_synced": True}).json()
    assert first["count"] == 1 and empty["count"] == 0
    assert rebuilt["count"] == 1
    assert sqlite_rows(path, "SELECT status FROM quiz_bank")[0]["status"] == "SYNCED_TO_MOBILE"


def test_pull_deck_stays_under_the_transport_ceiling(api):
    """Oversized banks are trimmed question-by-question, never split."""
    client, path = api
    bulk_questions(path, 260, filler=6000)
    deck = client.get("/api/sync/pull_deck", params={"limit": 500}).json()
    assert deck["bytes"] < MAX_DECK_BYTES
    assert 0 < deck["count"] < 260
    assert len(json.dumps(deck).encode()) < MAX_DECK_BYTES * 1.05


def test_export_deck_is_a_download_without_marking_anything(api):
    """Manual file export must not consume the staged pool."""
    client, path = api
    add_question(path)
    response = client.get("/api/sync/export_deck", params={"limit": 10})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.json()["count"] == 1
    assert sqlite_rows(path, "SELECT status FROM quiz_bank")[0]["status"] == "STAGED"


def test_pull_deck_rejects_an_unknown_mode(api):
    """Mode is a closed set; a typo must not silently serve everything."""
    client, path = api
    add_question(path)
    assert client.get("/api/sync/pull_deck", params={"mode": "subway"}).status_code == 422


# ------------------------------------------------------------- push reviews
def test_push_review_updates_logs_fsrs_status_and_momentum(api):
    """One upload touches flow_review_logs, fsrs_cards, fsrs_logs and quiz_bank."""
    client, path = api
    quiz_id = add_question(path, correct=2, cognitive="CALCULATION_STEP")
    before = sqlite_rows(path, "SELECT stability FROM fsrs_cards")[0]["stability"]

    response = client.post("/api/sync/push_reviews", json={
        "reviews": [review_body(quiz_id, user_selection=2, flow_rating=3,
                                context_mode="COMMUTE_TRAVEL",
                                error_category="NONE")],
        "session_id": "phone-1",
    })
    assert response.status_code == 200
    result = response.json()
    assert result["accepted"] == 1 and result["rejected"] == 0
    assert result["fsrs"][0]["stability"] > before

    log = sqlite_rows(path, "SELECT * FROM flow_review_logs")[0]
    assert log["quiz_id"] == quiz_id
    assert log["is_correct"] == 1
    assert log["flow_rating"] == 3 and log["fsrs_grade"] == 3
    assert log["context_mode"] == "COMMUTE_TRAVEL"
    assert log["synced_from_client"] == 1
    assert log["time_spent_seconds"] == 42

    assert sqlite_rows(path, "SELECT status FROM quiz_bank")[0]["status"] == "ANSWERED"
    assert len(sqlite_rows(path, "SELECT id FROM fsrs_logs")) == 1
    state = sqlite_rows(path, "SELECT momentum,answered,context_mode FROM flow_state")[0]
    assert state["momentum"] == pytest.approx(0.0)
    assert state["answered"] == 1
    assert state["context_mode"] == "COMMUTE_TRAVEL"


def test_push_review_marks_a_wrong_answer_as_incorrect(api):
    """Correctness is derived from the stored key, not trusted from the client."""
    client, path = api
    quiz_id = add_question(path, correct=0)
    result = client.post("/api/sync/push_reviews",
                         json={"reviews": [review_body(quiz_id, user_selection=3)]}).json()
    assert result["accepted"] == 1
    log = sqlite_rows(path, "SELECT is_correct FROM flow_review_logs")[0]
    assert log["is_correct"] == 0


def test_push_review_is_idempotent_by_client_id(api):
    """A phone that retries an upload must not double-count the review."""
    client, path = api
    quiz_id = add_question(path)
    body = {"reviews": [review_body(quiz_id, client_id="same-client")]}
    first = client.post("/api/sync/push_reviews", json=body).json()
    second = client.post("/api/sync/push_reviews", json=body).json()
    assert first["accepted"] == 1 and first["duplicates"] == 0
    assert second["accepted"] == 0 and second["duplicates"] == 1
    assert len(sqlite_rows(path, "SELECT id FROM flow_review_logs")) == 1
    assert len(sqlite_rows(path, "SELECT id FROM fsrs_logs")) == 1


def test_push_review_rolls_back_the_log_when_fsrs_fails(api, monkeypatch):
    """A card that could not advance must leave no review behind, so it retries."""
    client, path = api
    quiz_id = add_question(path)
    from engine import sync_bridge

    def boom(*args, **kwargs):
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(sync_bridge.fsrs_bridge, "apply_review", boom)
    result = client.post("/api/sync/push_reviews",
                         json={"reviews": [review_body(quiz_id)]}).json()
    assert result["accepted"] == 0 and result["rejected"] == 1
    assert "fsrs update failed" in result["errors"][0]
    assert sqlite_rows(path, "SELECT id FROM flow_review_logs") == []
    assert sqlite_rows(path, "SELECT status FROM quiz_bank")[0]["status"] == "STAGED"


def test_push_reviews_skips_an_unknown_quiz_without_failing_the_batch(api):
    """One stale id on a phone must not block the other reviews."""
    client, path = api
    quiz_id = add_question(path)
    result = client.post("/api/sync/push_reviews", json={"reviews": [
        review_body(9999, client_id="stale"),
        review_body(quiz_id, client_id="good"),
    ]}).json()
    assert result["accepted"] == 1 and result["rejected"] == 1
    assert "unknown quiz_id" in result["errors"][0]
    assert len(sqlite_rows(path, "SELECT id FROM flow_review_logs")) == 1


@pytest.mark.parametrize("override", [
    {"flow_rating": 0},
    {"flow_rating": 6},
    {"context_mode": "DRIVING"},
    {"time_spent_seconds": -5},
    {"quiz_id": 0},
])
def test_push_reviews_rejects_malformed_items(api, override):
    """Pydantic rejects the request before any database write."""
    client, path = api
    quiz_id = add_question(path)
    item = review_body(quiz_id)
    item.update(override)
    response = client.post("/api/sync/push_reviews", json={"reviews": [item]})
    assert response.status_code == 422
    assert sqlite_rows(path, "SELECT id FROM flow_review_logs") == []


def test_push_reviews_requires_a_non_empty_batch(api):
    """An empty upload is a client bug, not a no-op success."""
    client, path = api
    assert client.post("/api/sync/push_reviews", json={"reviews": []}).status_code == 422


# -------------------------------------------------------- postpone and flow
def test_postpone_endpoint_defers_the_question_for_a_day(api):
    """The 'solve later at desk' action moves due time with no lapse."""
    client, path = api
    quiz_id = add_question(path, cognitive="CALCULATION_STEP")
    before = sqlite_rows(path, "SELECT due FROM fsrs_cards")[0]["due"]
    response = client.post("/api/quiz/postpone", json={"quiz_id": quiz_id, "hours": 24})
    assert response.status_code == 200
    assert response.json()["postponed_hours"] == 24
    assert sqlite_rows(path, "SELECT status FROM quiz_bank")[0]["status"] == "POSTPONED"
    assert sqlite_rows(path, "SELECT due FROM fsrs_cards")[0]["due"] > before
    assert sqlite_rows(path, "SELECT id FROM fsrs_logs") == []


def test_postpone_unknown_quiz_is_a_404(api):
    """A stale deck on the phone gets a clear error, not a 500."""
    client, path = api
    assert client.post("/api/quiz/postpone", json={"quiz_id": 424242}).status_code == 404


def test_flow_endpoint_returns_the_tier_directive(api):
    """The UI can show the difficulty decision the server made."""
    client, path = api
    response = client.post("/api/quiz/flow", json={"flow_rating": 3, "session_id": "ui"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["momentum"] == 0.0
    assert payload["target_tier"] == 3
    assert payload["directive"] == "hold difficulty"


def test_queue_endpoint_interleaves_and_filters(api):
    """The queue endpoint exposes travel filtering to the browser."""
    client, path = api
    for index in range(4):
        add_question(path, cognitive="INTUITION", text=f"Mental {index} $x$?")
        add_question(path, cognitive="CALCULATION_STEP", text=f"Paper {index} $y$?")
    desk = client.get("/api/quiz/queue", params={"limit": 10, "mode": "all"}).json()
    assert all(isinstance(question["options"], list) for question in desk["questions"])
    assert all("options_json" not in question for question in desk["questions"])
    types = [question["cognitive_type"] for question in desk["questions"]]
    assert {"INTUITION", "CALCULATION_STEP"} <= set(types)
    assert all(left != right for left, right in zip(types, types[1:]))

    travel = client.get("/api/quiz/queue", params={"limit": 10, "mode": "travel"}).json()
    assert travel["questions"]
    assert all(question["requires_scratchpad"] == 0 for question in travel["questions"])


def test_generate_endpoint_validates_its_bounds(api):
    """A bad request never reaches the Gemini client."""
    client, path = api
    assert client.post("/api/quiz/generate", json={"count": 0}).status_code == 422
    status = client.get("/api/quiz/generate/status").json()
    assert status["running"] is False


def test_bank_routes_are_mounted_on_the_real_application(tmp_path, monkeypatch):
    """The study app, not only this test router, must expose the endpoints."""
    make_db(tmp_path)
    monkeypatch.setenv("RECALL_DB_PATH", str(tmp_path / "recall.sqlite"))
    module = importlib.import_module("00_main")
    paths = set(module.app.openapi()["paths"])
    assert {"/api/sync/pull_deck", "/api/sync/push_reviews", "/api/quiz/queue",
            "/api/quiz/stats", "/api/quiz/postpone", "/api/quiz/flow",
            "/api/sync/export_deck", "/manifest.webmanifest", "/sw.js"} <= paths
