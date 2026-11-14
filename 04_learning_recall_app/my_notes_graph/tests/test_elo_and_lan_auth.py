"""Elo ratings, seeded ranking, question recycling, sync state and LAN pairing."""
from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import auth, db, elo, flow_scheduler, fsrs_bridge, sync_bridge
from engine.routes import router
from tests.bank_fixtures import add_card, add_question, make_db, sqlite_rows

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    add_card(path)
    monkeypatch.setenv("RECALL_DB_PATH", str(path))
    return path


def review(quiz_id, selection, *, flow=3, client="c1", when=NOW):
    return {"quiz_id": quiz_id, "user_selection": selection, "time_spent_seconds": 5,
            "flow_rating": flow, "context_mode": "DESK_STANDARD",
            "error_category": None, "client_id": client, "reviewed_at": when.isoformat()}


# ------------------------------------------------------------------ model
def test_expected_has_a_guessing_floor_and_is_monotonic():
    assert elo.expected(0, 3000) == pytest.approx(0.25, abs=1e-3)
    assert elo.expected(3000, 0) == pytest.approx(1.0, abs=1e-3)
    assert elo.expected(1200, 1200) == pytest.approx(0.625)
    assert elo.expected(1300, 1200) > elo.expected(1200, 1200) > elo.expected(1100, 1200)


def test_decay_has_a_grace_day_a_half_life_and_a_floor():
    assert elo.decayed(1400, 0.5) == 1400
    assert elo.decayed(1400, 1.0) == 1400
    assert elo.decayed(1400, 1.0 + elo.HALF_LIFE_DAYS) == pytest.approx(1100)
    assert elo.decayed(1400, 10_000) == pytest.approx(elo.FLOOR, abs=1e-6)
    assert elo.decayed(700, 500) == 700            # never pulled *up* to the floor
    drops = [elo.decayed(1400, day) - elo.decayed(1400, day + 1) for day in range(1, 6)]
    assert all(drop > 0 for drop in drops)        # falls every idle day after grace


def test_guessing_has_zero_expected_drift_far_below_a_question():
    chance = elo.expected(800, 2400)
    drift = chance * (1 - chance) + (1 - chance) * (0 - chance)
    assert abs(drift) < 1e-9
    assert chance == pytest.approx(0.25, abs=1e-3)


# ---------------------------------------------------------------- recording
def test_correct_answer_raises_learner_and_lowers_question(bank):
    quiz_id = add_question(bank, correct=1)
    result = sync_bridge.push_reviews([review(quiz_id, 1)], path=str(bank))
    assert result["accepted"] == 1
    assert result["elo"]["overall_delta"] > 0
    assert result["elo"]["question_after"] < result["elo"]["question_before"]
    rows = {(row["scope"], row["key"]): row for row in
            sqlite_rows(bank, "SELECT scope,key,rating,games FROM elo_ratings")}
    assert rows[("learner", "*")]["rating"] > elo.BASE
    assert rows[("learner", "*")]["games"] == 1
    assert ("question", str(quiz_id)) in rows


def test_duplicate_upload_does_not_move_the_rating_twice(bank):
    quiz_id = add_question(bank, correct=1)
    sync_bridge.push_reviews([review(quiz_id, 1)], path=str(bank))
    before = sqlite_rows(bank, "SELECT rating,games FROM elo_ratings WHERE scope='learner' AND key='*'")[0]
    again = sync_bridge.push_reviews([review(quiz_id, 1)], path=str(bank))
    after = sqlite_rows(bank, "SELECT rating,games FROM elo_ratings WHERE scope='learner' AND key='*'")[0]
    assert again["duplicates"] == 1 and again["results"][0]["status"] == "duplicate"
    assert dict(before) == dict(after)


def test_wrong_answer_is_always_an_fsrs_lapse_even_if_it_felt_easy(bank):
    quiz_id = add_question(bank, correct=0)
    sync_bridge.push_reviews([review(quiz_id, 3, flow=1)], path=str(bank))
    log = sqlite_rows(bank, "SELECT is_correct,fsrs_grade FROM flow_review_logs")[0]
    assert log["is_correct"] == 0 and log["fsrs_grade"] == 1
    assert fsrs_bridge.grade_for_review(5, True) == 2
    assert fsrs_bridge.grade_for_review(1, True) == 4


def test_snapshot_reports_decay_without_writing_it(bank):
    quiz_id = add_question(bank, correct=1)
    sync_bridge.push_reviews([review(quiz_id, 1)], path=str(bank))
    fresh = elo.snapshot(str(bank), NOW.timestamp())
    later = elo.snapshot(str(bank), (NOW + timedelta(days=30)).timestamp())
    assert fresh["overall"]["lost_to_decay"] == 0
    assert later["overall"]["rating"] < fresh["overall"]["rating"]
    assert later["overall"]["lost_to_decay"] > 0
    again = elo.snapshot(str(bank), (NOW + timedelta(days=30)).timestamp())
    assert again["overall"] == later["overall"]     # reading twice decays once


def test_late_offline_review_never_moves_last_active_backwards(bank):
    first = add_question(bank, correct=1, text="one $a$?")
    second = add_question(bank, correct=1, text="two $b$?")
    sync_bridge.push_reviews([review(first, 1, client="a", when=NOW)], path=str(bank))
    sync_bridge.push_reviews([review(second, 1, client="b", when=NOW - timedelta(days=3))],
                             path=str(bank))
    row = sqlite_rows(bank, "SELECT last_active FROM elo_ratings WHERE scope='learner' AND key='*'")[0]
    assert row["last_active"] == pytest.approx(NOW.timestamp())


# ------------------------------------------------------------------ ranking
def test_seeded_queue_is_reproducible_and_differs_between_seeds(bank):
    for index in range(24):
        add_question(bank, cognitive=("INTUITION", "CALCULATION_STEP",
                                      "ADVERSARIAL_TRAP", "SYSTEM_APPLICATION")[index % 4],
                     text=f"Question {index} $x$?")
    order = lambda seed: [item["id"] for item in
                          flow_scheduler.candidates(path=str(bank), seed=seed)]
    assert order(7) == order(7)
    assert order(7) != order(8)
    assert sorted(order(7)) == sorted(order(8))                 # same pool, new order
    plain = [item["id"] for item in flow_scheduler.candidates(path=str(bank))]
    assert plain == [item["id"] for item in flow_scheduler.candidates(path=str(bank))]


def test_seeded_ranking_still_prefers_overdue_concepts_on_average(bank):
    add_card(bank, topic="Later", due_in_hours=24 * 30)
    due = [add_question(bank, text=f"due {index} $x$?") for index in range(6)]
    late = [add_question(bank, concept="Later", text=f"later {index} $x$?") for index in range(6)]
    wins = 0
    for seed in range(200):
        first = flow_scheduler.candidates(path=str(bank), seed=seed)[0]["id"]
        wins += first in due
    assert wins > 180, wins
    assert set(due).isdisjoint(late)


def test_answered_question_returns_only_when_due_and_rested(bank):
    quiz_id = add_question(bank, correct=1)
    sync_bridge.push_reviews([review(quiz_id, 1, when=datetime.now(timezone.utc))], path=str(bank))
    assert not flow_scheduler.candidates(path=str(bank), recycle=True)
    future = datetime.now(timezone.utc).timestamp() + 400 * 86400
    back = flow_scheduler.candidates(path=str(bank), recycle=True, now=future)
    assert [item["id"] for item in back] == [quiz_id] and back[0]["recycled"] is True
    assert not flow_scheduler.candidates(path=str(bank), recycle=False, now=future)


def test_sync_state_lists_answered_ids_and_ratings(bank):
    done = add_question(bank, correct=1, text="done $a$?")
    open_id = add_question(bank, correct=1, text="open $b$?")
    sync_bridge.push_reviews([review(done, 1, when=datetime.now(timezone.utc))], path=str(bank))
    state = sync_bridge.state(path=str(bank))
    assert state["answered_ids"] == [done] and open_id not in state["answered_ids"]
    assert state["elo"]["overall"]["games"] == 1
    assert state["reviews_logged"] == 1


def test_queue_and_elo_endpoints(bank):
    application = FastAPI()
    application.include_router(router)
    for index in range(8):
        add_question(bank, text=f"Q {index} $x$?")
    with TestClient(application) as client:
        first = client.get("/api/quiz/queue", params={"seed": 5}).json()
        second = client.get("/api/quiz/queue", params={"seed": 5}).json()
        plain = client.get("/api/quiz/queue", params={"shuffle": 0}).json()
        assert [q["id"] for q in first["questions"]] == [q["id"] for q in second["questions"]]
        assert first["seed"] == 5 and plain["seed"] is None
        assert "predicted_success" in first["questions"][0]
        assert client.get("/api/elo").json()["overall"]["rating"] == elo.BASE
        assert "answered_ids" in client.get("/api/sync/state").json()


# --------------------------------------------------------------- LAN pairing
@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB_PATH", str(tmp_path / "recall.sqlite"))
    monkeypatch.setenv("RECALL_TOKEN_PATH", str(tmp_path / "sync_token"))
    monkeypatch.delenv("RECALL_LAN_AUTH", raising=False)
    auth._failures.clear()
    main = importlib.import_module("00_main")
    return main.app


def lan(application, address="192.168.1.50"):
    return TestClient(application, client=(address, 50000), follow_redirects=False)


def test_loopback_needs_no_token_and_lan_does(guarded):
    with TestClient(guarded, client=("127.0.0.1", 50000)) as local:
        assert local.get("/api/quiz/stats").status_code == 200
        info = local.get("/api/network/info").json()
        assert info["pairing_code"] == auth.token() and info["auth_required"] is True
    with lan(guarded) as phone:
        assert phone.get("/api/health").status_code == 200
        assert phone.get("/api/quiz/stats").status_code == 401
        assert phone.get("/api/sync/pull_deck").status_code == 401
        assert phone.post("/api/sync/push_reviews", json={"reviews": []}).status_code == 401
        page = phone.get("/")
        assert page.status_code == 401 and "Pair this device" in page.text


def test_pairing_sets_a_cookie_that_unlocks_the_api(guarded):
    with lan(guarded) as phone:
        wrong = phone.get("/pair", params={"token": "nope"})
        assert wrong.status_code == 401
        paired = phone.get("/pair", params={"token": auth.token()})
        assert paired.status_code == 303 and paired.headers["location"] == "/"
        assert "HttpOnly" in paired.headers["set-cookie"]
        assert phone.get("/api/quiz/stats").status_code == 200
        info = phone.get("/api/network/info").json()
        assert "pairing_code" not in info and "pair_urls" not in info


def test_bearer_header_and_proxy_forwarding(guarded):
    with lan(guarded) as phone:
        ok = phone.get("/api/quiz/stats", headers={"Authorization": f"Bearer {auth.token()}"})
        assert ok.status_code == 200
    with TestClient(guarded, client=("127.0.0.1", 50000)) as proxied:
        # tailscale serve / nginx relay from loopback: must not be trusted.
        relayed = proxied.get("/api/quiz/stats", headers={"X-Forwarded-For": "100.101.102.103"})
        assert relayed.status_code == 401


def test_wrong_codes_are_rate_limited(guarded):
    with lan(guarded, "192.168.1.77") as phone:
        for _ in range(auth.MAX_FAILURES):
            assert phone.get("/pair", params={"token": "bad"}).status_code == 401
        assert phone.get("/pair", params={"token": auth.token()}).status_code == 429


def test_gate_can_be_disabled_and_token_file_is_private(guarded, monkeypatch, tmp_path):
    assert (auth.token_path().stat().st_mode & 0o777) == 0o600 if auth.token() else False
    monkeypatch.setenv("RECALL_LAN_AUTH", "0")
    with lan(guarded) as phone:
        assert phone.get("/api/quiz/stats").status_code == 200


def test_host_guard_accepts_tailscale_names_and_addresses(guarded):
    main = importlib.import_module("00_main")
    allowed = main.LanHostGuard._allowed
    assert allowed("100.101.102.103") and allowed("laptop.tail1234.ts.net")
    assert allowed("192.168.1.9") and allowed("recall.local")
    assert not allowed("example.com") and not allowed("8.8.8.8") and not allowed("100.128.0.1")
