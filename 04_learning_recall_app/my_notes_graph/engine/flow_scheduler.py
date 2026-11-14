"""Flow-state scheduler: interleaving, travel filtering and difficulty adaptation.

The queue is built from the *bank*, not from the FSRS card list, so a question
can be served while its concept card keeps its own memory schedule.  Three
behaviours are enforced here:

* no two consecutive questions share a cognitive type;
* travel mode serves only desk-free questions and pushes paper calculations a
  day forward without recording a lapse;
* session momentum nudges the requested difficulty tier up or down.
"""
from __future__ import annotations

import json
import random
import time
import zlib
from datetime import datetime, timezone

from . import db, elo, fsrs_bridge, interleaving

ROTATION = ("INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "SYSTEM_APPLICATION")
FLOW_DELTA = {1: 1.5, 2: 0.5, 3: 0.0, 4: -0.5, 5: -1.5}
MOMENTUM_DECAY = 0.7
MOMENTUM_GAIN = 0.3
UP_THRESHOLD = 1.0
DOWN_THRESHOLD = -1.0
BASE_TIER = 3
TRAVEL_POSTPONE_HOURS = 24.0
# Randomised ranking (only when a seed is supplied; the unseeded order is the
# original deterministic one).  Weights are on a common 0..1 scale per term.
W_BOOSTED = 3.0
W_OVERDUE = 2.0
W_ELO_MATCH = 1.0
W_TIER = 0.5
W_RECYCLED = -0.75
TEMPERATURE = 0.45
RECYCLE_REST_HOURS = 20.0


def daily_seed(session_id: str = "default", path: str | None = None,
               now: float | None = None) -> int:
    """Seed that is stable while a batch is being answered and new afterwards.

    It mixes the UTC day, the session and the number of logged reviews, so a
    page refresh keeps the order, the next batch gets a new one, and no two
    days repeat a sequence.
    """
    moment = time.time() if now is None else float(now)
    day = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d")
    connection = db.open_database(path)
    try:
        reviews = int(connection.execute("SELECT COUNT(*) FROM flow_review_logs").fetchone()[0])
    finally:
        connection.close()
    return zlib.crc32(f"{day}|{session_id}|{reviews}".encode())


def flow_delta(flow_rating: int) -> float:
    """Convert the five-point subjective rating into momentum change.

    Booleans and numeric strings are rejected instead of coerced: a rating is
    a deliberate UI choice, and a silent ``int("3")`` would hide a client bug.
    """
    if isinstance(flow_rating, bool) or not isinstance(flow_rating, int):
        raise ValueError("flow_rating must be an integer between 1 and 5")
    try:
        return FLOW_DELTA[flow_rating]
    except KeyError:
        raise ValueError("flow_rating must be an integer between 1 and 5") from None


def next_momentum(previous: float, flow_rating: int) -> float:
    """M(t+1) = 0.7·M(t) + 0.3·Δflow, the mission's DDA update rule."""
    return round(MOMENTUM_DECAY * float(previous) + MOMENTUM_GAIN * flow_delta(flow_rating), 4)


def tier_for_momentum(momentum: float, current: int = BASE_TIER) -> int:
    """Raise the target tier when the learner coasts, lower it when they drown."""
    if momentum > UP_THRESHOLD:
        return min(5, max(1, int(current) + 1))
    if momentum < DOWN_THRESHOLD:
        return max(1, min(5, int(current) - 1))
    return BASE_TIER if current not in range(1, 6) else int(current)


def _session(connection, session_id: str) -> dict:
    row = connection.execute(
        "SELECT session_id,momentum,target_tier,context_mode,answered FROM flow_state WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row:
        return dict(row)
    connection.execute(
        "INSERT INTO flow_state(session_id,momentum,target_tier,context_mode,answered) VALUES(?,0.0,3,'DESK_STANDARD',0)",
        (session_id,),
    )
    connection.commit()
    return {"session_id": session_id, "momentum": 0.0, "target_tier": BASE_TIER,
            "context_mode": "DESK_STANDARD", "answered": 0}


def record_rating(session_id: str = "default", flow_rating: int = 3,
                  *, context_mode: str | None = None, path: str | None = None) -> dict:
    """Update and persist session momentum after one answered question."""
    connection = db.open_database(path)
    try:
        state = _session(connection, session_id)
        momentum = next_momentum(state["momentum"], flow_rating)
        tier = tier_for_momentum(momentum, int(state["target_tier"]))
        connection.execute(
            """UPDATE flow_state SET momentum=?, target_tier=?, context_mode=?,
               answered=answered+1, updated_at=? WHERE session_id=?""",
            (momentum, tier,
             context_mode or state["context_mode"],
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             session_id),
        )
        connection.commit()
        return {"session_id": session_id, "momentum": momentum, "target_tier": tier,
                "previous_tier": int(state["target_tier"]),
                "flow_rating": int(flow_rating),
                "answered": int(state["answered"]) + 1,
                "directive": ("raise difficulty" if tier > int(state["target_tier"])
                              else "lower difficulty" if tier < int(state["target_tier"])
                              else "hold difficulty")}
    finally:
        connection.close()


def session_state(session_id: str = "default", path: str | None = None) -> dict:
    """Read the persisted momentum/tier of a session."""
    connection = db.open_database(path)
    try:
        return _session(connection, session_id)
    finally:
        connection.close()


def revive_postponed(*, path: str | None = None, now: float | None = None) -> int:
    """Return questions whose postponement has expired to the servable pool."""
    moment = time.time() if now is None else float(now)
    connection = db.open_database(path)
    try:
        cursor = connection.execute(
            """UPDATE quiz_bank SET status='STAGED', postponed_until=NULL
               WHERE status='POSTPONED' AND COALESCE(postponed_until,0)<=?""",
            (moment,),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def _due_of(connection, concept_id: str, now: float) -> float:
    row = connection.execute("SELECT due FROM fsrs_cards WHERE topic=?", (concept_id,)).fetchone()
    return float(row["due"]) if row else now


def candidates(*, mode: str = "all", session_id: str = "default",
               path: str | None = None, now: float | None = None,
               seed: int | None = None, recycle: bool = False) -> list[dict]:
    """Return servable questions ordered by urgency and tier distance.

    With ``seed`` the order is a seeded sample instead of a fixed sort: each
    question gets a score (failed-twice boost, FSRS overdue, how close its
    predicted success is to the session's target, tier distance) plus Gumbel
    noise, which samples without replacement in proportion to
    ``exp(score / TEMPERATURE)``.  Urgent material still comes first on
    average, but two sessions never see the same sequence.

    With ``recycle`` an already answered question returns once its concept is
    due again and it has rested ``RECYCLE_REST_HOURS``; otherwise a small bank
    runs dry and due concepts have nothing to be tested with.
    """
    moment = time.time() if now is None else float(now)
    revive_postponed(path=path, now=moment)
    connection = db.open_database(path)
    try:
        state = _session(connection, session_id)
        tier = int(state["target_tier"])
        clause = "q.status IN ('STAGED','SYNCED_TO_MOBILE') AND (q.postponed_until IS NULL OR q.postponed_until<=?)"
        params: list[object] = [moment]
        if mode == "travel":
            clause += " AND q.requires_scratchpad=0"
        rows = connection.execute(
            f"""SELECT q.*, COALESCE(f.due, ?) AS concept_due
                FROM quiz_bank q LEFT JOIN fsrs_cards f ON f.topic=q.concept_id
                WHERE {clause}""",
            [moment, *params],
        ).fetchall()
        items = [dict(row) for row in rows]
        if recycle:
            travel = " AND q.requires_scratchpad=0" if mode == "travel" else ""
            rested = moment - RECYCLE_REST_HOURS * 3600.0
            for row in connection.execute(
                    f"""SELECT q.*, f.due AS concept_due
                        FROM quiz_bank q JOIN fsrs_cards f ON f.topic=q.concept_id
                        WHERE q.status='ANSWERED' AND f.due<=?{travel}
                          AND NOT EXISTS (
                              SELECT 1 FROM flow_review_logs r WHERE r.quiz_id=q.id
                              AND (julianday(r.reviewed_at)-2440587.5)*86400.0>?)""",
                    (moment, rested)):
                item = dict(row)
                item["recycled"] = True
                items.append(item)
        for item in items:
            # The HTTP queue mirrors the deck payload: options are a list, not
            # a JSON string, so the browser never has to parse storage details.
            try:
                item["options"] = json.loads(item.pop("options_json", "[]") or "[]")
            except (TypeError, ValueError):
                item["options"] = []
        boosted = {str(row["concept_id"]) for row in connection.execute(
            """SELECT DISTINCT concept_id FROM session_weakness
               WHERE priority=1 AND resolved=1""")}
        if seed is None:
            items.sort(key=lambda item: (
                str(item["concept_id"]) not in boosted,
                item["concept_due"] > moment,
                float(item["concept_due"]),
                abs(int(item["difficulty_tier"]) - tier),
                int(item["id"]),
            ))
            return items
        generator = random.Random(int(seed))
        learned = elo.question_ratings(connection)
        target = elo.success_target(tier)
        learners: dict[str, float] = {}
        items.sort(key=lambda item: int(item["id"]))  # fixed draw order per seed
        for item in items:
            domain = interleaving.domain_of(str(item.get("topic_folder", "")))
            if domain not in learners:
                learners[domain] = elo.rating_for_domain(connection, domain, moment)
            rating = learned.get(int(item["id"]), elo.tier_rating(int(item["difficulty_tier"])))
            chance = elo.expected(learners[domain], rating)
            item["elo"] = round(rating)
            item["predicted_success"] = round(chance, 3)
            item["_score"] = (
                W_BOOSTED * (str(item["concept_id"]) in boosted)
                + W_OVERDUE * (float(item["concept_due"]) <= moment)
                + W_ELO_MATCH * elo.match_score(chance, target)
                + W_TIER * (1.0 - abs(int(item["difficulty_tier"]) - tier) / 4.0)
                + W_RECYCLED * bool(item.get("recycled"))
                + TEMPERATURE * elo.gumbel(generator.random())
            )
        items.sort(key=lambda item: (-item["_score"], int(item["id"])))
        for item in items:
            del item["_score"]
        return items
    finally:
        connection.close()


def interleave(items: list[dict], limit: int) -> list[dict]:
    """Order questions so no two consecutive items share a cognitive type.

    Within a type the caller's urgency order is preserved while the type order
    follows the INTUITION -> CALCULATION -> TRAP -> SYSTEM rotation, resuming
    just after the previous type and skipping types with nothing left.  A type
    that is the only one remaining is eventually served rather than starved.
    """
    buckets: dict[str, list[dict]] = {name: [] for name in ROTATION}
    for item in items:
        buckets.setdefault(str(item["cognitive_type"]), []).append(item)
    queue: list[dict] = []
    position = 0
    last = ""
    while len(queue) < limit and any(buckets.values()):
        choice = ""
        for step in range(len(ROTATION)):
            name = ROTATION[(position + step) % len(ROTATION)]
            if name != last and buckets.get(name):
                choice = name
                position = (position + step + 1) % len(ROTATION)
                break
        if not choice:
            # Only the previous type is left: take one to avoid starving it.
            choice = next(name for name in ROTATION if buckets.get(name))
            position = (ROTATION.index(choice) + 1) % len(ROTATION)
        queue.append(buckets[choice].pop(0))
        last = choice
    return queue


def apply_travel_postponement(path: str | None = None, *, hours: float = TRAVEL_POSTPONE_HOURS,
                              now: datetime | None = None) -> int:
    """Postpone every due paper-calculation question and its card by ``hours``.

    No FSRS lapse is recorded: the card keeps its stability and difficulty, only
    the due time moves.  Returns the number of questions postponed.
    """
    moment = now or datetime.now(timezone.utc)
    stamp = moment.timestamp()
    connection = db.open_database(path)
    try:
        rows = connection.execute(
            """SELECT id,concept_id FROM quiz_bank
               WHERE requires_scratchpad=1
                 AND status IN ('STAGED','SYNCED_TO_MOBILE')
                 AND (postponed_until IS NULL OR postponed_until<=?)""",
            (stamp,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE quiz_bank SET status='POSTPONED', postponed_until=? WHERE id=?",
                (stamp + hours * 3600.0, row["id"]),
            )
        connection.commit()
    finally:
        connection.close()
    for row in rows:
        fsrs_bridge.postpone(row["concept_id"], hours, path=path, now=moment)
    return len(rows)


def postpone_quiz(quiz_id: int, *, hours: float = TRAVEL_POSTPONE_HOURS,
                  path: str | None = None, record_lapse: bool = False) -> dict:
    """Defer one question (and its concept card) to the next desk session."""
    moment = datetime.now(timezone.utc)
    connection = db.open_database(path)
    try:
        row = connection.execute(
            "SELECT id,concept_id,status FROM quiz_bank WHERE id=?", (int(quiz_id),)).fetchone()
        if not row:
            raise ValueError("unknown quiz id")
        connection.execute(
            "UPDATE quiz_bank SET status='POSTPONED', postponed_until=? WHERE id=?",
            (moment.timestamp() + hours * 3600.0, int(quiz_id)),
        )
        connection.commit()
        concept = str(row["concept_id"])
    finally:
        connection.close()
    card = fsrs_bridge.postpone(concept, hours, path=path, now=moment) if not record_lapse else None
    return {"quiz_id": int(quiz_id), "concept_id": concept, "postponed_hours": hours,
            "due": card["due"] if card else None}


def build_queue(*, limit: int = 25, mode: str = "all", session_id: str = "default",
                path: str | None = None, postpone_travel: bool = True,
                seed: int | None = None, recycle: bool = False) -> dict:
    """Return the interleaved queue plus the session state that shaped it."""
    if mode not in ("all", "travel"):
        raise ValueError("mode must be 'all' or 'travel'")
    postponed = apply_travel_postponement(path) if mode == "travel" and postpone_travel else 0
    state = session_state(session_id, path)
    items = candidates(mode=mode, session_id=session_id, path=path,
                       seed=seed, recycle=recycle)
    queue = interleaving.build_session(items, max(1, int(limit)))
    return {
        "mode": mode,
        "seed": seed,
        "recycled": sum(1 for item in queue if item.get("recycled")),
        "session": state,
        "available": len(items),
        "postponed": postponed,
        "count": len(queue),
        "domains": interleaving.domain_counts(queue),
        "questions": queue,
    }


def stats(path: str | None = None) -> dict:
    """Question-bank and review counts for the sync modal."""
    connection = db.open_database(path)
    try:
        by_status = {row["status"]: row["n"] for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM quiz_bank GROUP BY status")}
        by_type = {row["cognitive_type"]: row["n"] for row in connection.execute(
            "SELECT cognitive_type, COUNT(*) AS n FROM quiz_bank GROUP BY cognitive_type")}
        reviews = int(connection.execute("SELECT COUNT(*) FROM flow_review_logs").fetchone()[0])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        reviewed_today = int(connection.execute(
            "SELECT COUNT(*) FROM flow_review_logs WHERE substr(reviewed_at,1,10)=?",
            (today,)).fetchone()[0])
        travel_free = int(connection.execute(
            "SELECT COUNT(*) FROM quiz_bank WHERE requires_scratchpad=0 AND status IN ('STAGED','SYNCED_TO_MOBILE')"
        ).fetchone()[0])
        return {
            "bank_total": sum(by_status.values()),
            "available": by_status.get("STAGED", 0),
            "synced_to_device": by_status.get("SYNCED_TO_MOBILE", 0),
            "completed": by_status.get("ANSWERED", 0),
            "postponed": by_status.get("POSTPONED", 0),
            "reviews_logged": reviews,
            "reviews_today": reviewed_today,
            "travel_ready": travel_free,
            "by_type": by_type,
        }
    finally:
        connection.close()
