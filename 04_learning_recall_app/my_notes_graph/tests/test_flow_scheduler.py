"""Flow scheduler: momentum DDA, cognitive interleaving and travel postponement."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from engine import flow_scheduler as scheduler
from engine import fsrs_bridge
from tests.bank_fixtures import add_card, add_question, make_db, now, sqlite_rows


# ------------------------------------------------------------------- momentum
@pytest.mark.parametrize("rating, delta", [(1, 1.5), (2, 0.5), (3, 0.0),
                                           (4, -0.5), (5, -1.5)])
def test_flow_delta_matches_the_five_point_scale(rating, delta):
    """Each subjective rating maps to the mission's Δflow."""
    assert scheduler.flow_delta(rating) == delta


@pytest.mark.parametrize("invalid", [0, 6, -1, "3", None, 2.5])
def test_flow_delta_rejects_out_of_range_ratings(invalid):
    """A rating outside 1-5 is a client bug, not a silent zero."""
    with pytest.raises(ValueError):
        scheduler.flow_delta(invalid)


def test_momentum_is_a_decaying_moving_average():
    """M(t+1) = 0.7·M(t) + 0.3·Δ, rounded for stable persistence."""
    assert scheduler.next_momentum(0.0, 1) == 0.45
    assert scheduler.next_momentum(0.45, 1) == 0.765
    assert scheduler.next_momentum(1.0, 3) == 0.7
    assert scheduler.next_momentum(-1.0, 5) == -1.15


def test_tier_rises_only_above_positive_one_and_falls_below_negative_one():
    """The DDA dead band keeps the tier stable between thresholds."""
    assert scheduler.tier_for_momentum(1.05, 3) == 4
    assert scheduler.tier_for_momentum(1.0, 3) == 3
    assert scheduler.tier_for_momentum(0.0, 3) == 3
    assert scheduler.tier_for_momentum(-1.05, 3) == 2
    assert scheduler.tier_for_momentum(-1.0, 1) == 1
    assert scheduler.tier_for_momentum(9.0, 5) == 5


def test_record_rating_persists_momentum_and_reports_the_directive(tmp_path):
    """Four 'too easy' answers in a row must lift the target tier."""
    path = make_db(tmp_path)
    assert scheduler.record_rating("s1", 1, path=str(path))["directive"] == "hold difficulty"
    scheduler.record_rating("s1", 1, path=str(path))
    scheduler.record_rating("s1", 1, path=str(path))
    fourth = scheduler.record_rating("s1", 1, context_mode="COMMUTE_TRAVEL", path=str(path))
    assert fourth["momentum"] == pytest.approx(1.1399, abs=1e-4)
    assert fourth["target_tier"] == 4
    assert fourth["directive"] == "raise difficulty"
    state = scheduler.session_state("s1", path=str(path))
    assert state["context_mode"] == "COMMUTE_TRAVEL"
    assert state["answered"] == 4


def test_record_rating_lowers_the_tier_when_overwhelmed(tmp_path):
    """Repeated 'overwhelmed' answers move the session back to easier tiers."""
    path = make_db(tmp_path)
    for _ in range(4):
        result = scheduler.record_rating("s2", 5, path=str(path))
    assert result["target_tier"] == 2
    assert result["directive"] == "lower difficulty"


# ----------------------------------------------------------------- interleave
def test_interleave_never_serves_the_same_type_twice_in_a_row():
    """The rotation covers all four types and alternates them."""
    items = []
    for index in range(12):
        items.append({"id": index, "cognitive_type": scheduler.ROTATION[index % 4]})
    queue = scheduler.interleave(items, 12)
    assert len(queue) == 12
    assert all(left["cognitive_type"] != right["cognitive_type"]
               for left, right in zip(queue, queue[1:]))


def test_interleave_serves_a_lone_type_rather_than_starving_it():
    """A pool with one type left still makes progress."""
    items = [{"id": 1, "cognitive_type": "INTUITION"},
             {"id": 2, "cognitive_type": "INTUITION"}]
    assert [item["id"] for item in scheduler.interleave(items, 5)] == [1, 2]


def test_interleave_is_bounded_by_the_requested_limit():
    """A 25-question request never returns the whole pool."""
    items = [{"id": index, "cognitive_type": scheduler.ROTATION[index % 4]} for index in range(40)]
    assert len(scheduler.interleave(items, 9)) == 9


# -------------------------------------------------------------- travel filter
def test_travel_candidates_exclude_every_scratchpad_question(tmp_path):
    """Mode=travel is a hard filter, not a ranking hint."""
    path = make_db(tmp_path)
    add_card(path)
    mental = add_question(path, cognitive="INTUITION", text="Mental question $1+1$?")
    paper = add_question(path, cognitive="CALCULATION_STEP", text="Paper question $\\int x dx$?")
    all_ids = {item["id"] for item in scheduler.candidates(mode="all", path=str(path))}
    travel_ids = {item["id"] for item in scheduler.candidates(mode="travel", path=str(path))}
    assert mental in travel_ids and paper not in travel_ids
    assert all_ids == {mental, paper}
    queue = scheduler.build_queue(limit=10, mode="travel", path=str(path))
    assert paper not in {item["id"] for item in queue["questions"]}


def test_travel_postpones_paper_questions_without_an_fsrs_lapse(tmp_path):
    """A commute must not damage stability; only the due time moves 24 h."""
    path = make_db(tmp_path)
    add_card(path, stability=4.0)
    add_question(path, cognitive="CALCULATION_STEP", text="Derive $A^{-1}$ for $2\\times2$.")
    before = sqlite_rows(path, "SELECT stability,due FROM fsrs_cards WHERE topic='Algebra'")[0]

    queue = scheduler.build_queue(limit=5, mode="travel", path=str(path))
    assert queue["postponed"] == 1
    row = sqlite_rows(path, "SELECT status,postponed_until FROM quiz_bank")[0]
    assert row["status"] == "POSTPONED"
    assert row["postponed_until"] == pytest.approx(time.time() + 24 * 3600, abs=120)

    after = sqlite_rows(path, "SELECT stability,due FROM fsrs_cards WHERE topic='Algebra'")[0]
    assert after["stability"] == pytest.approx(4.0)
    assert after["due"] == pytest.approx(before["due"] + 24 * 3600, abs=120)
    assert sqlite_rows(path, "SELECT id FROM fsrs_logs") == []
    assert sqlite_rows(path, "SELECT id FROM flow_review_logs") == []


def test_expired_postponements_return_to_the_queue(tmp_path):
    """Without revival a postponed question would disappear forever."""
    path = make_db(tmp_path)
    add_card(path)
    add_question(path, cognitive="CALCULATION_STEP")
    scheduler.postpone_quiz(1, path=str(path))
    assert scheduler.candidates(mode="all", path=str(path)) == []

    assert scheduler.revive_postponed(path=str(path), now=time.time() + 25 * 3600) == 1
    rows = sqlite_rows(path, "SELECT status,postponed_until FROM quiz_bank")
    assert rows[0]["status"] == "STAGED" and rows[0]["postponed_until"] is None
    assert len(scheduler.candidates(mode="all", path=str(path))) == 1


def test_postpone_quiz_moves_the_question_and_the_card_together(tmp_path):
    """The UI action defers the concept card too, with no rating recorded."""
    path = make_db(tmp_path)
    add_card(path, stability=2.5)
    quiz_id = add_question(path, cognitive="CALCULATION_STEP")
    result = scheduler.postpone_quiz(quiz_id, hours=12, path=str(path))
    assert result["quiz_id"] == quiz_id
    assert result["postponed_hours"] == 12
    assert sqlite_rows(path, "SELECT stability FROM fsrs_cards")[0]["stability"] == pytest.approx(2.5)
    assert sqlite_rows(path, "SELECT id FROM fsrs_logs") == []
    with pytest.raises(ValueError):
        scheduler.postpone_quiz(999, path=str(path))


def test_stats_reports_the_counts_the_sync_modal_shows(tmp_path):
    """The modal's X/Y/Z numbers come from one grouped query."""
    path = make_db(tmp_path)
    add_card(path)
    add_question(path, status="STAGED")
    add_question(path, status="SYNCED_TO_MOBILE", text="Second $x$?")
    add_question(path, status="ANSWERED", text="Third $x$?")
    add_question(path, status="POSTPONED", text="Fourth $x$?", postponed_until=time.time() + 3600)
    stats = scheduler.stats(path=str(path))
    assert stats["bank_total"] == 4
    assert stats["available"] == 1
    assert stats["synced_to_device"] == 1
    assert stats["completed"] == 1
    assert stats["postponed"] == 1
    assert stats["travel_ready"] >= 1


def test_build_queue_rejects_an_unknown_mode(tmp_path):
    """Only the two context modes exist; anything else is a client bug."""
    path = make_db(tmp_path)
    add_card(path)
    with pytest.raises(ValueError):
        scheduler.build_queue(mode="driving", path=str(path))


def test_fsrs_bridge_grade_mapping_is_stable():
    """The five-point rating always maps onto FSRS grades 1-4."""
    assert {rating: fsrs_bridge.grade_for_flow(rating) for rating in range(1, 6)} == \
        {1: 4, 2: 3, 3: 3, 4: 2, 5: 1}


def test_fsrs_bridge_review_advances_the_card(tmp_path):
    """A Good grade leaves the card scheduled further out than before."""
    path = make_db(tmp_path)
    add_card(path, stability=1.0)
    before = sqlite_rows(path, "SELECT due,stability FROM fsrs_cards")[0]
    result = fsrs_bridge.apply_review("Algebra", 3, path=str(path), reviewed_at=now())
    after = sqlite_rows(path, "SELECT due,stability FROM fsrs_cards")[0]
    assert after["stability"] > before["stability"]
    assert after["due"] > before["due"]
    assert result["grade"] == 3
    log = sqlite_rows(path, "SELECT rating,stability FROM fsrs_logs")
    assert len(log) == 1 and log[0]["rating"] == 3


def test_fsrs_bridge_postpone_does_not_create_a_log(tmp_path):
    """Postponement is a scheduling change, not evidence of forgetting."""
    path = make_db(tmp_path)
    add_card(path, stability=3.0)
    before = sqlite_rows(path, "SELECT due FROM fsrs_cards")[0]["due"]
    fsrs_bridge.postpone("Algebra", 24, path=str(path))
    after = sqlite_rows(path, "SELECT due,stability FROM fsrs_cards")[0]
    assert after["due"] == pytest.approx(before + 24 * 3600, abs=120)
    assert after["stability"] == pytest.approx(3.0)
    assert sqlite_rows(path, "SELECT id FROM fsrs_logs") == []
