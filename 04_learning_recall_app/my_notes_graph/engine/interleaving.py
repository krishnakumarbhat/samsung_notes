"""Cognitive interleaving and intra-session weakness reinforcement.

Two behaviours are enforced here:

* **Domain round-robin.**  A session walks the learner's note domains in a
  cycle (``maths/03. Algebra`` -> ``Mlaidlgenai_robo/DL`` -> ``Robotics`` ...)
  so no two consecutive questions come from the same domain while more than
  one domain still has candidates.
* **Cyclic reinforcement.**  A question that was missed, or rated *Challenging*
  / *Overwhelmed*, schedules a same-concept variant 8-12 slots later in the
  active queue.  A second failure marks the concept as high priority for the
  next session.

The module only depends on :mod:`engine.db`, so the scheduler, the HTTP routes
and the mobile sync bridge can all use it without an import cycle.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone

from . import db

ROTATION = ("INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "SYSTEM_APPLICATION")
TYPE_PREFERENCE = {"INTUITION": 0, "ADVERSARIAL_TRAP": 1,
                   "SYSTEM_APPLICATION": 2, "CALCULATION_STEP": 3}
WEAKNESS_MIN_GAP = 8
WEAKNESS_MAX_GAP = 12
BROAD_ROOTS = {"maths", "Mlaidlgenai_robo"}


def domain_of(topic_folder: str) -> str:
    """Return the study domain a question belongs to.

    The two broad roots hold many unrelated subjects, so their second path
    segment is the meaningful domain (``Mlaidlgenai_robo/DL`` rather than the
    whole ML/AI/Robotics collection).  Everything else uses its first segment.
    """
    parts = [part for part in str(topic_folder or "").replace("\\", "/").split("/") if part]
    if not parts:
        return "Uncategorized"
    if parts[0] in BROAD_ROOTS and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _cycle_types(items: list[dict]) -> list[dict]:
    """Order one domain's questions through the cognitive rotation."""
    buckets: dict[str, list[dict]] = {name: [] for name in ROTATION}
    for item in items:
        buckets.setdefault(str(item.get("cognitive_type", "")), []).append(item)
    ordered: list[dict] = []
    position = 0
    last = ""
    while any(buckets.values()):
        choice = ""
        for step in range(len(ROTATION)):
            name = ROTATION[(position + step) % len(ROTATION)]
            if name != last and buckets.get(name):
                choice = name
                position = (position + step + 1) % len(ROTATION)
                break
        if not choice:
            choice = next(name for name in ROTATION if buckets.get(name))
            position = (ROTATION.index(choice) + 1) % len(ROTATION)
        ordered.append(buckets[choice].pop(0))
        last = choice
    return ordered


def build_session(items: list[dict], limit: int) -> list[dict]:
    """Round-robin domains, rotating cognitive modes inside each domain.

    ``items`` is expected in the scheduler's urgency order; that order is kept
    within every domain.  When only one domain has candidates left it is served
    rather than starved, so the queue always reaches ``limit`` if the pool does.
    """
    if limit <= 0 or not items:
        return []
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(domain_of(str(item.get("topic_folder", ""))), []).append(item)
    queues = {domain: _cycle_types(group) for domain, group in groups.items()}
    order = list(queues)
    queue: list[dict] = []
    position = 0
    last_domain = ""
    last_type = ""
    while len(queue) < limit and any(queues.values()):
        choice = ""
        for step in range(len(order)):
            name = order[(position + step) % len(order)]
            if name != last_domain and queues[name]:
                choice = name
                position = (position + step + 1) % len(order)
                break
        if not choice:
            choice = next(name for name in order if queues[name])
            position = (order.index(choice) + 1) % len(order)
        bucket = queues[choice]
        index = next((offset for offset, item in enumerate(bucket)
                      if str(item.get("cognitive_type", "")) != last_type), 0)
        item = bucket.pop(index)
        queue.append(item)
        last_domain = choice
        last_type = str(item.get("cognitive_type", ""))
    return queue


def domain_counts(queue: list[dict]) -> dict[str, int]:
    """Count served questions per domain for the session summary."""
    counts: dict[str, int] = {}
    for item in queue:
        domain = domain_of(str(item.get("topic_folder", "")))
        counts[domain] = counts.get(domain, 0) + 1
    return counts


def _payload(row) -> dict:
    """Mirror the sync deck shape so the browser can render a variant directly."""
    try:
        options = json.loads(row["options_json"])
    except (TypeError, ValueError):
        options = []
    return {
        "id": int(row["id"]),
        "concept_id": row["concept_id"],
        "topic_folder": row["topic_folder"],
        "source_pdf": row["source_pdf"],
        "page_num": int(row["page_num"]),
        "cognitive_type": row["cognitive_type"],
        "requires_scratchpad": int(row["requires_scratchpad"]),
        "difficulty_tier": int(row["difficulty_tier"]),
        "question_md": row["question_md"],
        "options": options,
        "correct_index": int(row["correct_index"]),
        "explanation_md": row["explanation_md"],
    }


def _pick_variant(connection, source, *, exclude: int) -> object | None:
    """Choose the closest same-concept question with a different cognitive type."""
    servable = "status IN ('STAGED','SYNCED_TO_MOBILE') AND (postponed_until IS NULL OR postponed_until<=?)"
    rows = connection.execute(
        f"""SELECT * FROM quiz_bank
            WHERE concept_id=? AND id<>? AND cognitive_type<>? AND {servable}""",
        (source["concept_id"], exclude, source["cognitive_type"], time.time()),
    ).fetchall()
    if rows:
        return min(rows, key=lambda row: (
            TYPE_PREFERENCE.get(str(row["cognitive_type"]), 9),
            abs(int(row["difficulty_tier"]) - int(source["difficulty_tier"])),
            int(row["id"]),
        ))
    rows = connection.execute(
        f"""SELECT * FROM quiz_bank
            WHERE topic_folder=? AND concept_id<>? AND id<>? AND {servable}""",
        (source["topic_folder"], source["concept_id"], exclude, time.time()),
    ).fetchall()
    if not rows:
        return None
    return min(rows, key=lambda row: (
        TYPE_PREFERENCE.get(str(row["cognitive_type"]), 9),
        abs(int(row["difficulty_tier"]) - int(source["difficulty_tier"])),
        int(row["id"]),
    ))


def register_weakness(session_id: str, quiz_id: int, *, position: int = 0,
                      path: str | None = None, rng: random.Random | None = None) -> dict:
    """Schedule one same-concept variant 8-12 slots after the failed question.

    Repeating a failure for the same concept in one session reuses the already
    scheduled variant instead of flooding the queue with duplicates.
    """
    moment = time.time()
    generator = rng or random.Random()
    connection = db.open_database(path)
    try:
        source = connection.execute("SELECT * FROM quiz_bank WHERE id=?", (int(quiz_id),)).fetchone()
        if not source:
            raise ValueError("unknown quiz id")
        existing = connection.execute(
            """SELECT * FROM session_weakness
               WHERE session_id=? AND concept_id=? AND resolved=0
               ORDER BY id DESC LIMIT 1""",
            (session_id, source["concept_id"]),
        ).fetchone()
        if existing and existing["variant_quiz_id"]:
            variant = connection.execute(
                "SELECT * FROM quiz_bank WHERE id=?", (int(existing["variant_quiz_id"]),)).fetchone()
            if variant:
                return {"weakness_id": int(existing["id"]), "insert_after": int(existing["insert_after"]),
                        "question": _payload(variant), "reused": True, "priority": int(existing["priority"])}
        variant = _pick_variant(connection, source, exclude=int(quiz_id))
        if not variant:
            return {"weakness_id": None, "insert_after": 0, "question": None,
                    "reused": False, "priority": 0}
        gap = int(generator.randint(WEAKNESS_MIN_GAP, WEAKNESS_MAX_GAP))
        cursor = connection.execute(
            """INSERT INTO session_weakness
               (session_id,source_quiz_id,concept_id,variant_quiz_id,insert_after,
                position,resolved,priority,created_at)
               VALUES(?,?,?,?,?,?,0,0,?)""",
            (session_id, int(quiz_id), source["concept_id"], int(variant["id"]), gap,
             int(position), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
        return {"weakness_id": int(cursor.lastrowid), "insert_after": gap,
                "question": _payload(variant), "reused": False, "priority": 0}
    finally:
        connection.close()


def resolve_weakness(quiz_id: int, correct: bool, *, path: str | None = None) -> dict:
    """Close the reinforcement loop after the variant was answered.

    A correct second pass retires the weakness.  A second failure keeps it
    visible as a high-priority concept for the next session (FSRS has already
    scheduled the card from the grade the client sent).
    """
    connection = db.open_database(path)
    try:
        row = connection.execute(
            """SELECT * FROM session_weakness
               WHERE variant_quiz_id=? AND resolved=0 ORDER BY id DESC LIMIT 1""",
            (int(quiz_id),),
        ).fetchone()
        if not row:
            return {"resolved": False, "weakness_id": None, "priority": 0}
        priority = 0 if correct else 1
        connection.execute(
            "UPDATE session_weakness SET resolved=1, priority=? WHERE id=?",
            (priority, int(row["id"])),
        )
        connection.commit()
        return {"resolved": True, "weakness_id": int(row["id"]), "priority": priority,
                "concept_id": row["concept_id"]}
    finally:
        connection.close()


def priority_concepts(path: str | None = None, *, hours: float = 36.0) -> set[str]:
    """Concepts that failed twice recently, boosted in tomorrow's queue."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600.0
    connection = db.open_database(path)
    try:
        rows = connection.execute(
            """SELECT DISTINCT concept_id FROM session_weakness
               WHERE priority=1 AND resolved=1 AND created_at>=?""",
            (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),),
        ).fetchall()
        return {str(row["concept_id"]) for row in rows}
    finally:
        connection.close()
