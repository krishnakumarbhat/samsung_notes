"""Elo skill ratings for the learner and difficulty ratings for questions.

FSRS answers *when* a concept should come back.  Elo answers two different
questions that FSRS cannot:

* how hard is this question for this learner right now (so the queue can aim
  at a target success rate instead of a hand-labelled tier), and
* how sharp is the learner in each domain today (a number that falls when a
  domain is left alone, so the dashboard shows what is going stale).

Model
-----
A four-option question can be guessed, so the win expectancy has a floor::

    P(correct) = g + (1 - g) / (1 + 10 ** ((R_question - R_learner) / 400))

with ``g = 0.25``.  Both ratings move by ``K * (outcome - P)``; ``K`` shrinks
as evidence accumulates.  A lucky guess from far below a question therefore
moves almost nothing in expectation.

Daily decay
-----------
A learner rating is stored with the time of its last review.  The *effective*
rating is computed on read::

    R_eff = FLOOR + (R - FLOOR) * 0.5 ** (max(0, idle_days - GRACE) / HALF_LIFE)

Nothing has to run at midnight and replaying a sync batch cannot decay a
rating twice: decay is a pure function of the stored rating and idle time.
Question ratings never decay (a question does not get easier by waiting).
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

from . import db, interleaving

BASE = 1200.0
FLOOR = 800.0
GUESS = 0.25
HALF_LIFE_DAYS = 21.0
GRACE_DAYS = 1.0
DAY = 86400.0
OVERALL = "*"
TIER_RATING = {1: 900.0, 2: 1050.0, 3: 1200.0, 4: 1350.0, 5: 1500.0}
DOMAIN_MIN_GAMES = 5


def expected(learner: float, question: float) -> float:
    """Probability of a correct answer, with the four-option guessing floor."""
    return GUESS + (1.0 - GUESS) / (1.0 + 10.0 ** ((float(question) - float(learner)) / 400.0))


def decayed(rating: float, idle_days: float) -> float:
    """Pull a rating toward the floor after the grace period."""
    rating = float(rating)
    if rating <= FLOOR:
        return rating
    idle = max(0.0, float(idle_days) - GRACE_DAYS)
    return FLOOR + (rating - FLOOR) * 0.5 ** (idle / HALF_LIFE_DAYS)


def learner_k(games: int) -> float:
    """48 for a new rating, settling toward 16."""
    return 16.0 + 32.0 / (1.0 + max(0, int(games)) / 15.0)


def question_k(games: int) -> float:
    """Questions see few answers, so they start mobile and settle at 8."""
    return 8.0 + 24.0 / (1.0 + max(0, int(games)) / 5.0)


def tier_rating(tier: int) -> float:
    """Prior difficulty rating of a question nobody has answered yet."""
    return TIER_RATING.get(int(tier), BASE)


def _row(connection: sqlite3.Connection, scope: str, key: str):
    return connection.execute(
        "SELECT rating,games,last_active FROM elo_ratings WHERE scope=? AND key=?",
        (scope, key),
    ).fetchone()


def learner_rating(connection: sqlite3.Connection, key: str = OVERALL,
                   now: float | None = None) -> tuple[float, int]:
    """Effective (decayed) learner rating and the number of rated answers."""
    row = _row(connection, "learner", key)
    if not row:
        return BASE, 0
    moment = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    idle = max(0.0, moment - float(row["last_active"] or moment)) / DAY
    return decayed(float(row["rating"]), idle), int(row["games"])


def question_rating(connection: sqlite3.Connection, quiz_id: int, tier: int) -> tuple[float, int]:
    """Learned difficulty of a question, falling back to its tier prior."""
    row = _row(connection, "question", str(int(quiz_id)))
    if not row:
        return tier_rating(tier), 0
    return float(row["rating"]), int(row["games"])


def question_ratings(connection: sqlite3.Connection) -> dict[int, float]:
    """Every learned question rating in one query (for queue ranking)."""
    return {int(row["key"]): float(row["rating"]) for row in connection.execute(
        "SELECT key,rating FROM elo_ratings WHERE scope='question'")}


def rating_for_domain(connection: sqlite3.Connection, domain: str,
                      now: float | None = None) -> float:
    """Domain rating once it has evidence, the overall rating before that."""
    rating, games = learner_rating(connection, domain, now)
    if games >= DOMAIN_MIN_GAMES:
        return rating
    return learner_rating(connection, OVERALL, now)[0]


def _store(connection: sqlite3.Connection, scope: str, key: str, rating: float,
           games: int, last_active: float) -> None:
    connection.execute(
        """INSERT INTO elo_ratings(scope,key,rating,games,last_active) VALUES(?,?,?,?,?)
           ON CONFLICT(scope,key) DO UPDATE SET rating=excluded.rating,
               games=excluded.games, last_active=excluded.last_active""",
        (scope, key, float(rating), int(games), float(last_active)),
    )


def record(connection: sqlite3.Connection, *, quiz_id: int, topic_folder: str,
           difficulty_tier: int, correct: bool, when: datetime) -> dict:
    """Apply one answered question to the overall, domain and question ratings.

    The caller owns the transaction.  ``when`` is the device's review time; a
    late-arriving offline review never moves ``last_active`` backwards.
    """
    moment = when.timestamp()
    outcome = 1.0 if correct else 0.0
    domain = interleaving.domain_of(topic_folder)
    q_rating, q_games = question_rating(connection, quiz_id, difficulty_tier)
    result: dict = {"domain": domain, "question_before": round(q_rating, 1)}
    question_delta = 0.0
    for key in (OVERALL, domain):
        row = _row(connection, "learner", key)
        if row:
            last = float(row["last_active"] or moment)
            before = decayed(float(row["rating"]), max(0.0, moment - last) / DAY)
            games = int(row["games"])
        else:
            last, before, games = moment, BASE, 0
        chance = expected(before, q_rating)
        after = before + learner_k(games) * (outcome - chance)
        _store(connection, "learner", key, after, games + 1, max(last, moment))
        day = when.astimezone(timezone.utc).strftime("%Y-%m-%d")
        connection.execute(
            """INSERT INTO elo_daily(day,key,rating,reviews) VALUES(?,?,?,1)
               ON CONFLICT(day,key) DO UPDATE SET rating=excluded.rating,
                   reviews=elo_daily.reviews+1""",
            (day, key, after),
        )
        label = "overall" if key == OVERALL else "domain_rating"
        result[label] = round(after, 1)
        result[label + "_delta"] = round(after - before, 1)
        if key == OVERALL:
            result["expected"] = round(chance, 3)
            question_delta = question_k(q_games) * (chance - outcome)
    _store(connection, "question", str(int(quiz_id)), q_rating + question_delta,
           q_games + 1, moment)
    result["question_after"] = round(q_rating + question_delta, 1)
    return result


def snapshot(path: str | None = None, now: float | None = None) -> dict:
    """Ratings for the dashboard: today, what idleness has cost, and the trend."""
    moment = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    today = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d")
    connection = db.open_database(path)
    try:
        entries = []
        for row in connection.execute(
                "SELECT key,rating,games,last_active FROM elo_ratings WHERE scope='learner'"):
            last = float(row["last_active"] or moment)
            idle = max(0.0, moment - last) / DAY
            stored = float(row["rating"])
            current = decayed(stored, idle)
            tomorrow = decayed(stored, idle + 1.0)
            opened = connection.execute(
                "SELECT rating FROM elo_daily WHERE key=? AND day<? ORDER BY day DESC LIMIT 1",
                (row["key"], today)).fetchone()
            entries.append({
                "key": row["key"],
                "rating": round(current, 1),
                "peak_before_decay": round(stored, 1),
                "lost_to_decay": round(stored - current, 1),
                "drop_if_idle_tomorrow": round(current - tomorrow, 1),
                "idle_days": round(idle, 2),
                "games": int(row["games"]),
                "change_today": round(current - float(opened["rating"]), 1) if opened else None,
            })
        overall = next((entry for entry in entries if entry["key"] == OVERALL), None)
        domains = sorted((entry for entry in entries if entry["key"] != OVERALL),
                         key=lambda entry: (-entry["lost_to_decay"], entry["rating"]))
        history = [{"day": row["day"], "rating": round(float(row["rating"]), 1),
                    "reviews": int(row["reviews"])}
                   for row in connection.execute(
                       "SELECT day,rating,reviews FROM elo_daily WHERE key=? ORDER BY day DESC LIMIT 60",
                       (OVERALL,))]
        history.reverse()
        return {
            "overall": overall or {
                "key": OVERALL, "rating": BASE, "peak_before_decay": BASE,
                "lost_to_decay": 0.0, "drop_if_idle_tomorrow": 0.0, "idle_days": 0.0,
                "games": 0, "change_today": None},
            "domains": domains,
            "history": history,
            "model": {"base": BASE, "floor": FLOOR, "half_life_days": HALF_LIFE_DAYS,
                      "grace_days": GRACE_DAYS, "guess": GUESS},
        }
    finally:
        connection.close()


def success_target(tier: int) -> float:
    """Success rate the queue aims at; momentum raises the tier and lowers it.

    Tier 3 targets 80 %, close to the ~85 % rate that maximises learning speed
    for gradient-style learners (Wilson et al., 2019) while leaving room for
    desirable difficulty.
    """
    return min(0.92, max(0.60, 0.80 - 0.06 * (int(tier) - 3)))


def match_score(chance: float, target: float) -> float:
    """1 when the predicted success equals the target, 0 when 0.4 away."""
    return max(0.0, 1.0 - abs(float(chance) - float(target)) / 0.4)


def gumbel(uniform: float) -> float:
    """Gumbel(0,1) noise from a uniform draw; adding it then sorting samples
    without replacement in proportion to exp(score / temperature)."""
    clipped = min(1.0 - 1e-12, max(1e-12, float(uniform)))
    return -math.log(-math.log(clipped))
