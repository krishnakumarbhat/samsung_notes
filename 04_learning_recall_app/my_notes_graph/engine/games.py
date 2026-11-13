"""Topic-personalised study games built on the question bank.

Three games share the same source of truth: the notes already indexed in
``quiz_bank``.  A Sudoku board is personalised by mapping its nine digits onto
nine concepts from one topic folder, so the learner places *their* terms while
the puzzle stays a normal 9×9 Sudoku.  Trap Hunt reuses adversarial questions
for a timed combo round, and Concept Match pairs a concept with a one-line
explanation.

The Sudoku generator is deterministic for a given seed, carves cells while the
solution stays unique, and reports the number of givens it actually reached
(the requested difficulty is a target, not a promise).
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone

from . import db

DIFFICULTY_TARGET = {"easy": 42, "medium": 34, "hard": 28}
MIN_GIVENS = 17
GAMES = ("sudoku", "trap_hunt", "match")
HINT_LENGTH = 150
CLUE_LENGTH = 150


# --------------------------------------------------------------------- sudoku
def _masks(grid: list[int]):
    rows = [0] * 9
    cols = [0] * 9
    boxes = [0] * 9
    for index, value in enumerate(grid):
        if not value:
            continue
        row, col = divmod(index, 9)
        bit = 1 << (value - 1)
        rows[row] |= bit
        cols[col] |= bit
        boxes[(row // 3) * 3 + col // 3] |= bit
    return rows, cols, boxes


def _candidates(grid, rows, cols, boxes, index):
    row, col = divmod(index, 9)
    used = rows[row] | cols[col] | boxes[(row // 3) * 3 + col // 3]
    return [digit for digit in range(1, 10) if not used >> (digit - 1) & 1]


def _fill(grid: list[int], rng: random.Random) -> bool:
    """Fill an empty grid with a random complete solution (MRV backtracking)."""
    rows, cols, boxes = _masks(grid)
    order = list(range(81))
    rng.shuffle(order)

    def step() -> bool:
        best = -1
        best_candidates: list[int] = []
        for index in order:
            if grid[index]:
                continue
            candidates = _candidates(grid, rows, cols, boxes, index)
            if not candidates:
                return False
            if best < 0 or len(candidates) < len(best_candidates):
                best, best_candidates = index, candidates
                if len(candidates) == 1:
                    break
        if best < 0:
            return True
        row, col = divmod(best, 9)
        box = (row // 3) * 3 + col // 3
        rng.shuffle(best_candidates)
        for digit in best_candidates:
            bit = 1 << (digit - 1)
            grid[best] = digit
            rows[row] |= bit
            cols[col] |= bit
            boxes[box] |= bit
            if step():
                return True
            grid[best] = 0
            rows[row] &= ~bit
            cols[col] &= ~bit
            boxes[box] &= ~bit
        return False

    return step()


def count_solutions(grid: list[int], limit: int = 2) -> int:
    """Count solutions up to ``limit``; used to guarantee a unique puzzle."""
    rows, cols, boxes = _masks(grid)
    work = list(grid)

    def step() -> int:
        best = -1
        best_candidates: list[int] = []
        for index in range(81):
            if work[index]:
                continue
            candidates = _candidates(work, rows, cols, boxes, index)
            if not candidates:
                return 0
            if best < 0 or len(candidates) < len(best_candidates):
                best, best_candidates = index, candidates
                if len(candidates) == 1:
                    break
        if best < 0:
            return 1
        row, col = divmod(best, 9)
        box = (row // 3) * 3 + col // 3
        total = 0
        for digit in best_candidates:
            bit = 1 << (digit - 1)
            work[best] = digit
            rows[row] |= bit
            cols[col] |= bit
            boxes[box] |= bit
            total += step()
            work[best] = 0
            rows[row] &= ~bit
            cols[col] &= ~bit
            boxes[box] &= ~bit
            if total >= limit:
                break
        return total

    return step()


def _carve(solution: list[int], target: int, rng: random.Random) -> list[int]:
    """Remove cells in symmetric pairs while the solution stays unique."""
    puzzle = list(solution)
    givens = 81
    seen: set[int] = set()
    groups: list[tuple[int, ...]] = []
    order = list(range(81))
    rng.shuffle(order)
    for index in order:
        mirror = 80 - index
        if index in seen:
            continue
        seen.add(index)
        seen.add(mirror)
        groups.append((index,) if mirror == index else (index, mirror))

    for group in groups:
        if givens - len(group) < max(target, MIN_GIVENS):
            continue
        backup = [puzzle[index] for index in group]
        for index in group:
            puzzle[index] = 0
        if count_solutions(puzzle, 2) == 1:
            givens -= len(group)
        else:
            for index, value in zip(group, backup):
                puzzle[index] = value

    # A second, symmetry-breaking pass gets closer to the requested count.
    if givens > target:
        for index in rng.sample(range(81), 81):
            if givens <= target:
                break
            if not puzzle[index]:
                continue
            value = puzzle[index]
            puzzle[index] = 0
            if count_solutions(puzzle, 2) == 1:
                givens -= 1
            else:
                puzzle[index] = value
    return puzzle


def generate_sudoku(*, seed: int | None = None, difficulty: str = "medium") -> dict:
    """Build a complete grid and carve a uniquely solvable puzzle."""
    level = difficulty if difficulty in DIFFICULTY_TARGET else "medium"
    chosen = int(seed) if seed is not None else random.SystemRandom().randrange(1, 2**31)
    rng = random.Random(chosen)
    solution = [0] * 81
    if not _fill(solution, rng):
        raise RuntimeError("could not build a Sudoku solution")
    puzzle = _carve(solution, DIFFICULTY_TARGET[level], rng)
    return {
        "difficulty": level,
        "seed": chosen,
        "puzzle": [puzzle[row * 9:(row + 1) * 9] for row in range(9)],
        "solution": [solution[row * 9:(row + 1) * 9] for row in range(9)],
        "givens": sum(1 for value in puzzle if value),
        "target_givens": DIFFICULTY_TARGET[level],
    }


# ---------------------------------------------------------------------- topics
def _short(label: str, used: set[str]) -> str:
    """A unique 2-3 character abbreviation for the symbol mode."""
    letters = re.sub(r"[^A-Za-z0-9]", "", label).upper() or "T"
    for size in (2, 3, 4):
        candidate = letters[:size]
        if candidate not in used:
            used.add(candidate)
            return candidate
    suffix = 2
    while True:
        candidate = (letters[:2] + str(suffix))[:4]
        if candidate not in used:
            used.add(candidate)
            return candidate
        suffix += 1


def _clean(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^(intuition|derivation|trap|step-by-step derivation)\s*:\s*",
                   "", value, flags=re.I)
    return value[:limit].rstrip()


def _legend(concepts: list[dict], fallback: list[dict] | None = None) -> list[dict]:
    """Map digits 1-9 onto the topic's concepts, filling from other notes.

    A folder with three concepts still deserves a board that feels personal, so
    the remaining digits borrow distinct concepts from the rest of the bank
    before falling back to neutral ``Term N`` labels.
    """
    entries = list(concepts[:9])
    used_terms = {str(entry.get("concept") or "") for entry in entries}
    for entry in fallback or []:
        if len(entries) >= 9:
            break
        term = str(entry.get("concept") or "")
        if term and term not in used_terms:
            entries.append(entry)
            used_terms.add(term)
    while len(entries) < 9:
        entries.append({"concept": f"Term {len(entries) + 1}", "hint": ""})
    used: set[str] = set()
    legend = []
    for digit, entry in enumerate(entries, 1):
        term = str(entry.get("concept") or f"Term {digit}")
        legend.append({
            "digit": digit,
            "term": term,
            "short": _short(term, used),
            "hint": _clean(entry.get("hint", ""), HINT_LENGTH),
        })
    return legend


def _concepts(connection, topic: str) -> list[dict]:
    clause = ""
    params: list[object] = []
    if topic:
        clause = "WHERE COALESCE(NULLIF(topic_folder,''),'(unfiled)')=?"
        params.append(topic)
    rows = connection.execute(
        f"""SELECT concept_id, COUNT(*) AS n, MAX(explanation_md) AS hint
            FROM quiz_bank {clause}
            GROUP BY concept_id ORDER BY n DESC, concept_id ASC""",
        params,
    ).fetchall()
    return [{"concept": row["concept_id"], "hint": row["hint"] or ""} for row in rows]


def topics(path: str | None = None) -> list[dict]:
    """List topic folders with the concepts that personalise a board."""
    connection = db.open_database(path)
    try:
        rows = connection.execute(
            """SELECT COALESCE(NULLIF(topic_folder,''),'(unfiled)') AS topic,
                      COUNT(*) AS questions
               FROM quiz_bank GROUP BY topic ORDER BY questions DESC, topic ASC"""
        ).fetchall()
        result = []
        for row in rows:
            concepts = _concepts(connection, str(row["topic"]))
            result.append({
                "topic": row["topic"],
                "questions": int(row["questions"]),
                "concepts": concepts[:9],
            })
        if result:
            return result
        # No generated questions yet: fall back to the FSRS concepts so the
        # game still feels personal instead of showing nine generic terms.
        cards = connection.execute(
            "SELECT topic, folder FROM fsrs_cards ORDER BY topic ASC").fetchall()
        if cards:
            by_folder: dict[str, list[dict]] = {}
            for card in cards:
                folder = str(card["folder"] or "(unfiled)")
                by_folder.setdefault(folder, []).append(
                    {"concept": str(card["topic"]), "hint": ""})
            return [{"topic": folder, "questions": len(concepts),
                     "concepts": concepts[:9]}
                    for folder, concepts in by_folder.items()]
        return [{"topic": "", "questions": 0, "concepts": []}]
    finally:
        connection.close()


def sudoku(topic: str = "", *, difficulty: str = "medium",
           seed: int | None = None, path: str | None = None) -> dict:
    """Return a unique puzzle plus the topic legend that personalises it."""
    connection = db.open_database(path)
    try:
        concepts = _concepts(connection, topic)
        fallback = _concepts(connection, "") if topic else []
    finally:
        connection.close()
    board = generate_sudoku(seed=seed, difficulty=difficulty)
    legend = _legend(concepts, fallback)
    own = {str(entry.get("concept") or "") for entry in concepts}
    from_topic = sum(1 for entry in legend if entry["term"] in own)
    return {"topic": topic, "legend": legend, "from_topic": from_topic,
            "mixed": from_topic < len(legend), **board}


# ------------------------------------------------------------------ trap hunt
def _trap_rows(connection, topic: str):
    """Desk-free adversarial rows for one topic (or the whole bank)."""
    clause = "cognitive_type='ADVERSARIAL_TRAP' AND requires_scratchpad=0"
    params: list[object] = []
    if topic:
        clause += " AND COALESCE(NULLIF(topic_folder,''),'(unfiled)')=?"
        params.append(topic)
    return connection.execute(
        f"""SELECT id,concept_id,topic_folder,question_md,options_json,
                   correct_index,explanation_md,difficulty_tier
            FROM quiz_bank WHERE {clause} ORDER BY id ASC""",
        params,
    ).fetchall()


def trap_hunt(topic: str = "", limit: int = 12, *, seed: int | None = None,
              path: str | None = None) -> dict:
    """Serve desk-free adversarial questions for a timed combo round.

    A folder with only one or two traps is topped up from other topics so the
    60-second round is still worth starting; ``mixed`` tells the UI to say so.
    """
    connection = db.open_database(path)
    try:
        rows = list(_trap_rows(connection, topic))
        mixed = False
        if topic and 0 < len(rows) < min(5, limit):
            known = {int(row["id"]) for row in rows}
            for row in _trap_rows(connection, ""):
                if len(rows) >= limit:
                    break
                if int(row["id"]) not in known:
                    rows.append(row)
                    known.add(int(row["id"]))
                    mixed = True
    finally:
        connection.close()
    rng = random.Random(seed if seed is not None else time.time_ns())
    picked = rng.sample([dict(row) for row in rows], min(max(1, limit), len(rows))) \
        if rows else []
    questions = []
    for row in picked:
        try:
            options = json.loads(row["options_json"])
        except (TypeError, ValueError):
            options = []
        if len(options) != 4:
            continue
        questions.append({
            "id": int(row["id"]),
            "concept_id": row["concept_id"],
            "topic_folder": row["topic_folder"],
            "question_md": row["question_md"],
            "options": options,
            "correct_index": int(row["correct_index"]),
            "explanation_md": row["explanation_md"],
            "difficulty_tier": int(row["difficulty_tier"]),
        })
    return {"topic": topic, "count": len(questions), "questions": questions,
            "mixed": mixed}


# --------------------------------------------------------------- concept match
def _match_candidates(connection, topic: str) -> list[dict]:
    """Distinct concept/clue pairs, preferring the first explanation per concept."""
    clause = "explanation_md<>''"
    params: list[object] = []
    if topic:
        clause += " AND COALESCE(NULLIF(topic_folder,''),'(unfiled)')=?"
        params.append(topic)
    rows = connection.execute(
        f"""SELECT concept_id,question_md,explanation_md FROM quiz_bank
            WHERE {clause} ORDER BY id ASC""",
        params,
    ).fetchall()
    seen: set[str] = set()
    candidates = []
    for row in rows:
        concept = str(row["concept_id"])
        if concept in seen:
            continue
        clue = _clean(row["explanation_md"], CLUE_LENGTH)
        if len(clue) < 20:
            clue = _clean(row["question_md"], CLUE_LENGTH)
        if len(clue) < 20:
            continue
        seen.add(concept)
        candidates.append({"concept": concept, "clue": clue})
    return candidates


def match_pairs(topic: str = "", pairs: int = 6, *, seed: int | None = None,
                path: str | None = None) -> dict:
    """Return concept/clue pairs; clues are the first line of the explanation.

    When a folder holds fewer concepts than a full board, the rest are borrowed
    from other topics so the game stays playable instead of dealing one pair.
    """
    connection = db.open_database(path)
    try:
        candidates = _match_candidates(connection, topic)
        mixed = False
        if topic and len(candidates) < pairs:
            known = {entry["concept"] for entry in candidates}
            for entry in _match_candidates(connection, ""):
                if len(candidates) >= pairs:
                    break
                if entry["concept"] not in known:
                    candidates.append(entry)
                    known.add(entry["concept"])
                    mixed = True
    finally:
        connection.close()

    rng = random.Random(seed if seed is not None else time.time_ns())
    picked = rng.sample(candidates, min(max(1, pairs), len(candidates))) \
        if candidates else []
    cards = []
    for index, entry in enumerate(picked):
        cards.append({"pair": index, "kind": "concept", "text": entry["concept"]})
        cards.append({"pair": index, "kind": "clue", "text": entry["clue"]})
    rng.shuffle(cards)
    return {"topic": topic, "pairs": len(picked), "cards": cards, "mixed": mixed}


# -------------------------------------------------------------------- scoring
def record_score(game: str, *, topic: str = "", score: int = 0, accuracy: float = 0.0,
                 duration_seconds: int = 0, detail: dict | None = None,
                 client_id: str | None = None, path: str | None = None) -> dict:
    """Store one finished round and return the personal best for that game."""
    if game not in GAMES:
        raise ValueError("unknown game")
    score = int(score)
    if score < 0 or score > 1_000_000:
        raise ValueError("score out of range")
    accuracy = max(0.0, min(1.0, float(accuracy)))
    duration_seconds = max(0, min(86_400, int(duration_seconds)))
    blob = json.dumps(detail or {}, separators=(",", ":"))
    if len(blob) > 4096:
        raise ValueError("detail payload too large")
    connection = db.open_database(path)
    try:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO game_scores
            (game,topic,score,accuracy,duration_seconds,detail_json,client_id,played_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (game, topic, score, accuracy, duration_seconds,
             blob, client_id,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        connection.commit()
        scope = "game=?"
        params: list[object] = [game]
        if topic:
            scope += " AND topic=?"
            params.append(topic)
        best = connection.execute(
            f"SELECT MAX(score) AS best FROM game_scores WHERE {scope}", params).fetchone()
        return {"stored": bool(cursor.rowcount), "best": int(best["best"] or 0),
                "score": score}
    finally:
        connection.close()


def scores(path: str | None = None, *, topic: str = "") -> dict:
    """Personal bests per game plus the five most recent rounds."""
    connection = db.open_database(path)
    try:
        clause = "WHERE topic=?" if topic else ""
        params: list[object] = [topic] if topic else []
        best = {row["game"]: {"best": int(row["best"] or 0), "rounds": int(row["rounds"])}
                for row in connection.execute(
                    f"""SELECT game, MAX(score) AS best, COUNT(*) AS rounds
                        FROM game_scores {clause} GROUP BY game""", params)}
        recent = [dict(row) for row in connection.execute(
            f"""SELECT game,topic,score,accuracy,duration_seconds,played_at
                FROM game_scores {clause} ORDER BY id DESC LIMIT 5""", params)]
        return {"best": best, "recent": recent}
    finally:
        connection.close()
