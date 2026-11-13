"""Topic-personalised study games: Sudoku, Trap Hunt and Concept Match.

The Sudoku generator is the only part with real algorithmic risk, so it is
checked structurally (valid complete grid, puzzle subset, unique solution,
determinism) instead of snapshotting a board.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import games
from engine.routes import router
from tests.bank_fixtures import add_card, add_question, make_db, sqlite_rows


@pytest.fixture()
def bank(tmp_path):
    """A small maths bank with two topics and one desk-free trap."""
    path = make_db(tmp_path)
    add_question(path, concept="Eigenvalues", cognitive="ADVERSARIAL_TRAP",
                 scratchpad=0, text="Is every eigenvalue real?", options=["yes", "no", "sometimes", "never"],
                 correct=1, folder="linear-algebra", explanation="Only for symmetric real matrices.")
    add_question(path, concept="Rank", cognitive="ADVERSARIAL_TRAP", scratchpad=0,
                 text="Does rank equal the number of rows?", folder="linear-algebra",
                 explanation="Rank is the dimension of the column space.")
    add_question(path, concept="Chain rule", cognitive="INTUITION", scratchpad=0,
                 text="What does the chain rule compute?", folder="calculus",
                 explanation="The derivative of a composition of functions.")
    add_question(path, concept="Paper trap", cognitive="ADVERSARIAL_TRAP",
                 scratchpad=1, text="Expand the determinant by hand?", folder="calculus",
                 explanation="Needs paper, so it must never appear in Trap Hunt.")
    return path


@pytest.fixture()
def api(bank, monkeypatch):
    """The games router on its own app and its own database."""
    monkeypatch.setenv("RECALL_DB_PATH", str(bank))
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as client:
        yield client, bank


def assert_valid_solution(grid) -> None:
    """A complete Sudoku grid has 1-9 exactly once per row, column and box."""
    assert len(grid) == 9 and all(len(row) == 9 for row in grid)
    for row in grid:
        assert sorted(row) == list(range(1, 10))
    for column in range(9):
        assert sorted(grid[row][column] for row in range(9)) == list(range(1, 10))
    for top in (0, 3, 6):
        for left in (0, 3, 6):
            box = [grid[row][column] for row in range(top, top + 3)
                   for column in range(left, left + 3)]
            assert sorted(box) == list(range(1, 10))


# --------------------------------------------------------------------- sudoku
def test_sudoku_solution_is_complete_and_puzzle_unique():
    """The carved puzzle keeps exactly one solution and every given is right."""
    board = games.generate_sudoku(seed=4242, difficulty="medium")
    assert_valid_solution(board["solution"])
    puzzle = [value for row in board["puzzle"] for value in row]
    solution = [value for row in board["solution"] for value in row]
    assert games.count_solutions(puzzle, 2) == 1
    assert board["givens"] == sum(1 for value in puzzle if value)
    assert board["givens"] >= games.MIN_GIVENS
    for index, value in enumerate(puzzle):
        if value:
            assert value == solution[index]


def test_sudoku_is_deterministic_and_difficulty_changes_givens():
    """A seed reproduces the board; harder boards expose fewer givens."""
    first = games.generate_sudoku(seed=7, difficulty="medium")
    second = games.generate_sudoku(seed=7, difficulty="medium")
    assert first["puzzle"] == second["puzzle"]
    assert first["solution"] == second["solution"]
    easy = games.generate_sudoku(seed=11, difficulty="easy")
    hard = games.generate_sudoku(seed=11, difficulty="hard")
    assert easy["givens"] > hard["givens"]
    assert easy["target_givens"] == games.DIFFICULTY_TARGET["easy"]
    assert hard["target_givens"] == games.DIFFICULTY_TARGET["hard"]


def test_sudoku_unknown_difficulty_falls_back_to_medium():
    """A typo in the query string must not crash the board."""
    board = games.generate_sudoku(seed=5, difficulty="impossible")
    assert board["difficulty"] == "medium"


def test_sudoku_legend_fills_sparse_topics_from_other_notes(bank):
    """Sparse notes still produce a complete digit legend."""
    board = games.sudoku("linear-algebra", seed=9, path=bank)
    legend = board["legend"]
    assert [entry["digit"] for entry in legend] == list(range(1, 10))
    assert legend[0]["term"] == "Eigenvalues"
    assert legend[1]["term"] == "Rank"
    assert board["from_topic"] == 2
    assert board["mixed"] is True
    # Other folders' concepts fill the board before neutral "Term N" labels.
    assert legend[2]["term"] in {"Chain rule", "Paper trap"}
    assert all(not entry["term"].startswith("Term ") for entry in legend[:4])
    shorts = [entry["short"] for entry in legend]
    assert len(set(shorts)) == 9
    assert all(1 < len(short) <= 4 for short in shorts)


def test_sudoku_legend_puts_the_topic_first(bank):
    """A calculus board starts with calculus concepts, even when it mixes."""
    board = games.sudoku("calculus", seed=9, path=bank)
    terms = [entry["term"] for entry in board["legend"]]
    assert terms[0] == "Chain rule"
    assert board["from_topic"] == 2
    assert board["mixed"] is True


def test_sudoku_legend_is_pure_when_a_topic_has_nine_concepts(tmp_path):
    """Nine distinct concepts mean no borrowing and no generic padding."""
    path = make_db(tmp_path)
    for index in range(9):
        add_question(path, concept=f"Concept {index}", folder="pure",
                     text=f"Question {index}?", explanation=f"Explanation {index}.")
    board = games.sudoku("pure", seed=12, path=path)
    assert board["from_topic"] == 9
    assert board["mixed"] is False
    assert all(not entry["term"].startswith("Term ") for entry in board["legend"])


def test_topics_lists_folders_with_their_concepts(bank):
    """The hub menu is built from the question bank itself."""
    topics = {entry["topic"]: entry for entry in games.topics(bank)}
    assert topics["linear-algebra"]["questions"] == 2
    assert [concept["concept"] for concept in topics["linear-algebra"]["concepts"]] \
        == ["Eigenvalues", "Rank"]
    assert topics["calculus"]["questions"] == 2


def test_topics_falls_back_to_fsrs_cards(tmp_path):
    """Before any question exists the game still feels personal."""
    path = make_db(tmp_path)
    add_card(path, topic="Vectors", folder="linear-algebra")
    add_card(path, topic="Limits", folder="calculus")
    topics = {entry["topic"]: entry for entry in games.topics(path)}
    assert set(topics) == {"linear-algebra", "calculus"}
    assert topics["calculus"]["concepts"][0]["concept"] == "Limits"


# ------------------------------------------------------------------ trap hunt
def test_trap_hunt_only_serves_desk_free_adversarial_questions(bank):
    """Paper questions and non-trap questions are filtered out server-side."""
    payload = games.trap_hunt(limit=12, seed=1, path=bank)
    assert payload["count"] == 2
    ids = {row["id"] for row in payload["questions"]}
    paper = sqlite_rows(bank, "SELECT id FROM quiz_bank WHERE requires_scratchpad=1")
    assert ids.isdisjoint({row["id"] for row in paper})
    for row in payload["questions"]:
        assert row["question_md"] != "What does the chain rule compute?"
        assert len(row["options"]) == 4
        assert 0 <= row["correct_index"] <= 3


def test_trap_hunt_respects_limit_and_topic(bank):
    """The round length is bounded and the topic filter is exact."""
    payload = games.trap_hunt("linear-algebra", limit=1, seed=2, path=bank)
    assert payload["count"] == 1
    assert payload["questions"][0]["topic_folder"] == "linear-algebra"
    assert games.trap_hunt("missing-topic", limit=5, path=bank)["count"] == 0


def test_trap_hunt_tops_up_a_thin_topic(bank):
    """A one-trap folder borrows traps from other notes for a real round."""
    add_question(bank, concept="Limits", cognitive="ADVERSARIAL_TRAP", scratchpad=0,
                 text="Is 0/0 defined?", folder="stats",
                 explanation="Indeterminate until a limit is taken.")
    payload = games.trap_hunt("stats", limit=5, seed=5, path=bank)
    assert payload["mixed"] is True
    assert payload["count"] >= 2
    topics = {row["topic_folder"] for row in payload["questions"]}
    assert "stats" in topics and len(topics) > 1


# --------------------------------------------------------------- concept match
def test_match_pairs_builds_two_cards_per_pair(bank):
    """Each pair is one concept plus one clue, shuffled together."""
    payload = games.match_pairs("linear-algebra", pairs=2, seed=3, path=bank)
    assert payload["pairs"] == 2
    assert len(payload["cards"]) == 4
    by_pair = {}
    for card in payload["cards"]:
        assert card["kind"] in ("concept", "clue")
        by_pair.setdefault(card["pair"], set()).add(card["kind"])
        assert card["text"]
    assert all(kinds == {"concept", "clue"} for kinds in by_pair.values())
    concepts = {card["text"] for card in payload["cards"] if card["kind"] == "concept"}
    assert concepts == {"Eigenvalues", "Rank"}


def test_match_pairs_never_duplicates_a_concept(bank):
    """Repeated concepts collapse before sampling."""
    payload = games.match_pairs(pairs=12, seed=4, path=bank)
    concepts = [card["text"] for card in payload["cards"] if card["kind"] == "concept"]
    assert len(concepts) == len(set(concepts))
    assert payload["pairs"] == len(concepts)


def test_match_fills_a_thin_topic_from_other_notes(bank):
    """One-concept folders still deal a playable board, clearly marked mixed."""
    payload = games.match_pairs("calculus", pairs=3, seed=4, path=bank)
    assert payload["pairs"] == 3
    assert payload["mixed"] is True
    concepts = {card["text"] for card in payload["cards"] if card["kind"] == "concept"}
    assert "Chain rule" in concepts
    assert len(concepts) == 3


# -------------------------------------------------------------------- scoring
def test_record_score_keeps_the_personal_best(bank):
    """Rounds accumulate; the best is scoped to the game and topic."""
    first = games.record_score("sudoku", topic="calculus", score=120, accuracy=0.9,
                               duration_seconds=180, path=bank)
    assert first == {"stored": True, "best": 120, "score": 120}
    second = games.record_score("sudoku", topic="calculus", score=300, accuracy=0.8,
                                duration_seconds=200, path=bank)
    assert second["best"] == 300
    games.record_score("sudoku", topic="linear-algebra", score=50, path=bank)
    assert games.record_score("sudoku", topic="calculus", score=10, path=bank)["best"] == 300
    rows = sqlite_rows(bank, "SELECT game,topic,score,accuracy FROM game_scores ORDER BY id")
    assert len(rows) == 4
    assert rows[1]["score"] == 300 and rows[1]["accuracy"] == pytest.approx(0.8)


def test_record_score_is_idempotent_for_client_id(bank):
    """A retried offline upload stores exactly one row."""
    body = dict(game="trap_hunt", topic="calculus", score=90, client_id="phone-1")
    assert games.record_score(path=bank, **body)["stored"] is True
    assert games.record_score(path=bank, **body)["stored"] is False
    assert len(sqlite_rows(bank, "SELECT id FROM game_scores")) == 1


def test_record_score_rejects_bad_input(bank):
    """Unknown games and out-of-range values are refused before SQL."""
    with pytest.raises(ValueError):
        games.record_score("chess", path=bank)
    with pytest.raises(ValueError):
        games.record_score("sudoku", score=2_000_000, path=bank)
    with pytest.raises(ValueError):
        games.record_score("sudoku", detail={"blob": "x" * 5000}, path=bank)
    assert sqlite_rows(bank, "SELECT id FROM game_scores") == []


def test_scores_groups_bests_and_recent_rounds(bank):
    """The hub reads one grouped snapshot."""
    games.record_score("sudoku", topic="calculus", score=100, path=bank)
    games.record_score("sudoku", topic="calculus", score=400, path=bank)
    games.record_score("match", topic="calculus", score=250, path=bank)
    snapshot = games.scores(bank, topic="calculus")
    assert snapshot["best"]["sudoku"] == {"best": 400, "rounds": 2}
    assert snapshot["best"]["match"]["best"] == 250
    assert len(snapshot["recent"]) == 3
    assert snapshot["recent"][0]["game"] == "match"
    assert games.scores(bank, topic="linear-algebra")["best"] == {}


# ------------------------------------------------------------------------ api
def test_games_api_serves_topics_and_boards(api):
    """Every hub endpoint answers with the documented shape."""
    client, _ = api
    topics = client.get("/api/games/topics").json()["topics"]
    assert {entry["topic"] for entry in topics} == {"linear-algebra", "calculus"}

    board = client.get("/api/games/sudoku", params={"topic": "calculus", "seed": 1}).json()
    assert board["topic"] == "calculus"
    assert len(board["puzzle"]) == 9
    assert board["legend"][0]["term"] == "Chain rule"

    traps = client.get("/api/games/trap_hunt", params={"topic": "linear-algebra", "limit": 5}).json()
    assert traps["count"] == 2

    match = client.get("/api/games/match", params={"topic": "calculus", "pairs": 3}).json()
    assert len(match["cards"]) == 2 * match["pairs"]

    scores = client.get("/api/games/scores").json()
    assert scores == {"best": {}, "recent": []}


def test_games_api_validates_sudoku_query(api):
    """Difficulty is a closed enum; a bad value is a 422, not a 500."""
    client, _ = api
    assert client.get("/api/games/sudoku", params={"difficulty": "impossible"}).status_code == 422
    assert client.get("/api/games/sudoku", params={"seed": -1}).status_code == 422
    assert client.get("/api/games/sudoku", params={"topic": "x" * 201}).status_code == 422
    assert client.get("/api/games/trap_hunt", params={"limit": 0}).status_code == 422


def test_games_api_stores_and_reads_a_score(api):
    """The offline queue uploads the same payload shape the API accepts."""
    client, _ = api
    body = {"game": "sudoku", "topic": "calculus", "score": 420,
            "accuracy": 0.75, "duration_seconds": 240,
            "detail": {"difficulty": "medium", "mistakes": 2}, "client_id": "web-abc"}
    response = client.post("/api/games/score", json=body)
    assert response.status_code == 200
    assert response.json() == {"stored": True, "best": 420, "score": 420}
    assert client.post("/api/games/score", json=body).json()["stored"] is False
    snapshot = client.get("/api/games/scores", params={"topic": "calculus"}).json()
    assert snapshot["best"]["sudoku"] == {"best": 420, "rounds": 1}
    assert snapshot["recent"][0]["accuracy"] == pytest.approx(0.75)


@pytest.mark.parametrize("override", [
    {"game": "chess"},
    {"score": 1_000_001},
    {"accuracy": 1.5},
    {"duration_seconds": -1},
])
def test_games_api_rejects_invalid_scores(api, override):
    """Pydantic bounds keep junk out of the table."""
    client, bank = api
    body = {"game": "sudoku", "topic": "calculus", "score": 10}
    body.update(override)
    assert client.post("/api/games/score", json=body).status_code == 422
    assert sqlite_rows(bank, "SELECT id FROM game_scores") == []
