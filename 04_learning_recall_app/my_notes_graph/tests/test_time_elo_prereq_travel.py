"""Answer-time weighted Elo, prerequisite fallback + leech guard, travel mode."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from engine import elo, flow_scheduler, fsrs_bridge, interleaving, sync_bridge
from tests.bank_fixtures import add_card, add_question, make_db, sqlite_rows


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    add_card(path)
    monkeypatch.setenv("RECALL_DB_PATH", str(path))
    return path


def review(quiz_id, selection, *, seconds=5, client="c1", flow=3, context="DESK_STANDARD"):
    return {"quiz_id": quiz_id, "user_selection": selection, "time_spent_seconds": seconds,
            "flow_rating": flow, "context_mode": context, "error_category": None,
            "client_id": client, "reviewed_at": datetime.now(timezone.utc).isoformat()}


def set_page(path, quiz_id, page):
    connection = sqlite3.connect(path)
    connection.execute("UPDATE quiz_bank SET page_num=? WHERE id=?", (page, quiz_id))
    connection.commit()
    connection.close()


# ------------------------------------------------------------------ answer-time Elo
def test_speed_factor_shape():
    ref = elo.reference_seconds("INTUITION")
    assert elo.speed_factor(True, ref, ref) == pytest.approx(1.0)
    assert elo.speed_factor(True, ref / 2, ref) == pytest.approx(1.25)
    assert elo.speed_factor(True, ref / 10, ref) == pytest.approx(1.25)      # capped
    assert elo.speed_factor(True, ref * 2, ref) == pytest.approx(0.75)
    assert elo.speed_factor(True, ref * 20, ref) == pytest.approx(0.75)      # capped
    assert elo.speed_factor(True, 0, ref) == 1.0                              # unknown time
    assert elo.speed_factor(False, 1, ref) == 1.0                             # wrong: never scaled
    assert elo.reference_seconds("CALCULATION_STEP", True) > elo.reference_seconds("CALCULATION_STEP")


def _gain(tmp_path, name, seconds, selection=1):
    path = make_db(tmp_path / name)
    add_card(path)
    quiz_id = add_question(path, correct=1)
    result = sync_bridge.push_reviews([review(quiz_id, selection, seconds=seconds)], path=str(path))
    return result["elo"]


def test_fast_correct_earns_more_than_slow_correct(tmp_path):
    for name in ("fast", "ref", "slow", "wfast", "wslow"):
        (tmp_path / name).mkdir()
    fast, ref, slow = (_gain(tmp_path, n, s) for n, s in (("fast", 5), ("ref", 20), ("slow", 90)))
    assert fast["overall_delta"] > ref["overall_delta"] > slow["overall_delta"] > 0
    assert fast["overall_delta"] == pytest.approx(1.25 * ref["overall_delta"], rel=0.02)
    assert slow["overall_delta"] == pytest.approx(0.75 * ref["overall_delta"], rel=0.02)
    assert fast["speed_factor"] == 1.25 and slow["speed_factor"] == 0.75
    # the question's own difficulty update does not depend on answer time
    assert fast["question_after"] == ref["question_after"] == slow["question_after"]
    # a wrong answer costs the same whether it was fast or slow
    wrong_fast, wrong_slow = _gain(tmp_path, "wfast", 3, 2), _gain(tmp_path, "wslow", 200, 2)
    assert wrong_fast["overall_delta"] == wrong_slow["overall_delta"] < 0


# ------------------------------------------------------------------ prerequisite + leech
def test_first_miss_schedules_a_variant(bank):
    missed = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="q1")
    variant = add_question(bank, concept="Eigen", cognitive="INTUITION", text="q2")
    out = interleaving.register_weakness("s1", missed, path=str(bank), correct=False)
    assert out["remediation"] == "variant" and out["question"]["id"] == variant
    assert out["failures_in_session"] == 1 and out["insert_after"] >= 8


def test_second_miss_in_session_steps_back_to_the_prerequisite(bank):
    prereq = add_question(bank, concept="Determinant", text="p", cognitive="INTUITION")
    missed = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="q1")
    variant = add_question(bank, concept="Eigen", cognitive="INTUITION", text="q2")
    later = add_question(bank, concept="Jordan", text="later")
    for quiz_id, page in ((prereq, 3), (missed, 7), (variant, 7), (later, 12)):
        set_page(bank, quiz_id, page)
    first = interleaving.register_weakness("s1", missed, path=str(bank), correct=False)
    assert first["remediation"] == "variant"
    second = interleaving.register_weakness("s1", variant, path=str(bank), correct=False)
    assert second["remediation"] == "prerequisite"
    assert second["question"]["id"] == prereq                    # earlier page, never the later one
    assert second["question"]["concept_id"] == "Determinant"
    assert second["insert_after"] == interleaving.REMEDIATION_GAP and second["priority"] == 1
    again = interleaving.register_weakness("s1", variant, path=str(bank), correct=False)
    assert again["reused"] and again["question"]["id"] == prereq   # no duplicates in the queue


def test_effortful_correct_answer_never_climbs_the_ladder(bank):
    prereq = add_question(bank, concept="Determinant", text="p")
    missed = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="q1")
    add_question(bank, concept="Eigen", cognitive="INTUITION", text="q2")
    set_page(bank, prereq, 1); set_page(bank, missed, 5)
    interleaving.register_weakness("s1", missed, path=str(bank), correct=False)
    out = interleaving.register_weakness("s1", missed, path=str(bank), correct=True)
    assert out["remediation"] == "variant"


def test_failures_are_counted_from_uploaded_reviews_too(bank):
    prereq = add_question(bank, concept="Determinant", text="p")
    a = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="q1", correct=0)
    b = add_question(bank, concept="Eigen", cognitive="ADVERSARIAL_TRAP", text="q2", correct=0)
    set_page(bank, prereq, 1); set_page(bank, a, 5); set_page(bank, b, 5)
    sync_bridge.push_reviews([review(a, 3, client="x1"), review(b, 3, client="x2")], path=str(bank))
    out = interleaving.register_weakness("fresh-session", b, path=str(bank), correct=False)
    assert out["failures_in_session"] == 2 and out["remediation"] == "prerequisite"


def test_leech_guard_serves_the_simplest_intuition_card_after_four_lapses(bank):
    hard = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="hard", correct=0, tier=4)
    easy = add_question(bank, concept="Eigen", cognitive="INTUITION", text="easy", tier=1, status="ANSWERED")
    add_question(bank, concept="Eigen", cognitive="INTUITION", text="mid", tier=3)
    sync_bridge.push_reviews([review(hard, 3, client=f"l{i}") for i in range(4)], path=str(bank))
    out = interleaving.register_weakness("s1", hard, path=str(bank), correct=False)
    assert out["remediation"] == "intuition" and out["lapses"] >= 4
    assert out["question"]["id"] == easy                          # lowest tier, answered cards allowed
    leech = interleaving.leeches(str(bank))
    assert leech[0]["concept_id"] == "Eigen" and leech[0]["needs_simplified"] == 0
    assert flow_scheduler.stats(str(bank))["leeches"] == 1


def test_leech_without_an_intuition_card_is_flagged_for_the_factory(bank):
    prereq = add_question(bank, concept="Determinant", text="p")
    hard = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="hard", correct=0)
    set_page(bank, prereq, 1); set_page(bank, hard, 4)
    sync_bridge.push_reviews([review(hard, 3, client=f"l{i}") for i in range(4)], path=str(bank))
    out = interleaving.register_weakness("s1", hard, path=str(bank), correct=False)
    assert out["remediation"] == "prerequisite"                   # falls through to the next rung
    stats = flow_scheduler.stats(str(bank))
    assert stats["leeches"] == 1 and stats["leeches_needing_simpler_card"] == 1


def test_weakness_endpoint_accepts_the_outcome(bank):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engine import routes
    application = FastAPI()
    application.include_router(routes.router)
    missed = add_question(bank, concept="Eigen", cognitive="CALCULATION_STEP", text="q1")
    add_question(bank, concept="Eigen", cognitive="INTUITION", text="q2")
    with TestClient(application) as client:
        body = client.post("/api/quiz/weakness", json={"quiz_id": missed, "session_id": "s", "correct": False}).json()
        assert body["remediation"] == "variant"
        legacy = client.post("/api/quiz/weakness", json={"quiz_id": missed, "session_id": "t"}).json()
        assert legacy["question"] is not None                     # old clients still work


# ------------------------------------------------------------------ travel mode
def test_travel_mode_hides_paper_questions_and_postpones_without_a_lapse(bank):
    mental = add_question(bank, concept="Algebra", cognitive="INTUITION", text="mental")
    paper = add_question(bank, concept="Algebra", cognitive="CALCULATION_STEP", text="paper")
    before = fsrs_bridge.card_state("Algebra", str(bank))
    logs_before = sqlite_rows(bank, "SELECT COUNT(*) AS n FROM fsrs_logs")[0]["n"]
    queue = flow_scheduler.build_queue(mode="travel", path=str(bank))
    ids = [item["id"] for item in queue["questions"]]
    assert mental in ids and paper not in ids
    row = sqlite_rows(bank, "SELECT status,postponed_until FROM quiz_bank WHERE id=?", (paper,))[0]
    assert row["status"] == "POSTPONED" and row["postponed_until"] is not None
    after = fsrs_bridge.card_state("Algebra", str(bank))
    assert after == before                                        # no FSRS change: not a lapse
    assert sqlite_rows(bank, "SELECT COUNT(*) AS n FROM fsrs_logs")[0]["n"] == logs_before
    assert sqlite_rows(bank, "SELECT COUNT(*) AS n FROM flow_review_logs")[0]["n"] == 0
    # back at the desk, once the postponement has run out, the paper question returns
    connection = sqlite3.connect(bank)
    connection.execute("UPDATE quiz_bank SET postponed_until=1 WHERE id=?", (paper,))
    connection.commit(); connection.close()
    desk = flow_scheduler.build_queue(mode="all", path=str(bank))
    assert paper in [item["id"] for item in desk["questions"]]


def test_paper_only_concept_is_postponed_once_and_keeps_its_memory_state(bank):
    add_question(bank, concept="Algebra", cognitive="CALCULATION_STEP", text="paper 1")
    add_question(bank, concept="Algebra", cognitive="CALCULATION_STEP", text="paper 2")
    before = fsrs_bridge.card_state("Algebra", str(bank))
    assert flow_scheduler.build_queue(mode="travel", path=str(bank))["postponed"] == 2
    after = fsrs_bridge.card_state("Algebra", str(bank))
    moved = after.pop("due") - before.pop("due")
    assert 23.9 * 3600 <= moved <= 24.1 * 3600                    # once, not once per question
    assert after == before                                        # stability, difficulty, rating intact
    assert sqlite_rows(bank, "SELECT COUNT(*) AS n FROM fsrs_logs")[0]["n"] == 0
