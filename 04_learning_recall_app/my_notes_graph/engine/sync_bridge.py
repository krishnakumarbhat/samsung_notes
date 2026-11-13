"""Mobile sync bridge: bundle a deck out, accept review logs back.

The payload is deliberately self-contained: a phone that downloaded a deck can
grade offline, show the explanation, and upload the log later.  Nothing in the
deck needs a second request, and every upload is idempotent through the
``client_id`` the device generates per review.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

from . import db, flow_scheduler, fsrs_bridge, interleaving

MAX_DECK_BYTES = 2_000_000
MAX_BATCH_BYTES = 1_500_000
DEFAULT_DECK_LIMIT = 100
ERROR_CATEGORIES = {"ALGEBRAIC_SLIP", "CONCEPTUAL_GAP", "TRAP_OVERLOOKED", "NONE", None}


def _question_payload(row) -> dict:
    try:
        options = json.loads(row["options_json"])
    except Exception:
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


def _deck_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


def pull_deck(limit: int = DEFAULT_DECK_LIMIT, mode: str = "all", *,
              path: str | None = None, mark: bool = True,
              include_synced: bool = False, max_bytes: int = MAX_DECK_BYTES) -> dict:
    """Bundle up to ``limit`` staged questions, staying under the size ceiling.

    ``include_synced`` rebuilds a deck a device has already received (for
    example after clearing its browser storage) without touching the status of
    answered questions.
    """
    if mode not in ("all", "travel"):
        raise ValueError("mode must be 'all' or 'travel'")
    limit = max(1, min(int(limit), 500))
    postponed = flow_scheduler.apply_travel_postponement(path) if mode == "travel" else 0
    connection = db.open_database(path)
    try:
        statuses = "('STAGED','SYNCED_TO_MOBILE')" if include_synced else "('STAGED')"
        clause = f"status IN {statuses} AND (postponed_until IS NULL OR postponed_until<=?)"
        params: list[object] = [time.time()]
        if mode == "travel":
            clause += " AND requires_scratchpad=0"
        rows = connection.execute(
            f"""SELECT * FROM quiz_bank WHERE {clause}
                ORDER BY difficulty_tier ASC, id ASC LIMIT ?""",
            [*params, limit],
        ).fetchall()
        questions = [_question_payload(row) for row in rows]
        payload = {
            "deck_id": "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "count": len(questions),
            "questions": questions,
        }
        # Keep the whole deck under the transport ceiling; a question is atomic.
        while questions and _deck_bytes(payload) > max_bytes:
            questions.pop()
            payload["questions"] = questions
            payload["count"] = len(questions)
        digest = hashlib.sha256(
            json.dumps([question["id"] for question in questions]).encode()
            + payload["generated_at"].encode()).hexdigest()[:16]
        payload["deck_id"] = digest
        if mark and questions:
            connection.executemany(
                "UPDATE quiz_bank SET status='SYNCED_TO_MOBILE' WHERE id=?",
                [(question["id"],) for question in questions],
            )
            connection.commit()
        payload["bytes"] = _deck_bytes(payload)
        payload["postponed"] = postponed
        return payload
    finally:
        connection.close()


def pull_batch(count: int = 100, mode: str = "all", *, path: str | None = None,
               mark: bool = True) -> dict:
    """LAN-friendly batch: at most 1.5 MB, sized for one phone request."""
    return pull_deck(limit=count, mode=mode, path=path, mark=mark,
                     max_bytes=MAX_BATCH_BYTES)


def _validate_review(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("review must be an object")
    quiz_id = int(item.get("quiz_id", 0))
    if quiz_id <= 0:
        raise ValueError("quiz_id is required")
    selection = item.get("user_selection")
    if selection is not None and (not isinstance(selection, int) or not 0 <= selection <= 3):
        raise ValueError("user_selection must be 0-3")
    rating = item.get("flow_rating")
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("flow_rating must be 1-5")
    spent = item.get("time_spent_seconds", 0)
    if not isinstance(spent, int) or spent < 0 or spent > 86400:
        raise ValueError("time_spent_seconds must be 0-86400")
    context = str(item.get("context_mode", "DESK_STANDARD"))
    if context not in ("DESK_STANDARD", "COMMUTE_TRAVEL"):
        raise ValueError("context_mode must be DESK_STANDARD or COMMUTE_TRAVEL")
    category = item.get("error_category")
    if category not in ERROR_CATEGORIES:
        raise ValueError("error_category is not recognised")
    client_id = item.get("client_id")
    if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 120):
        raise ValueError("client_id must be a short string")
    reviewed_at = item.get("reviewed_at") or datetime.now(timezone.utc).isoformat()
    try:
        moment = datetime.fromisoformat(str(reviewed_at).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("reviewed_at must be ISO-8601") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return {
        "quiz_id": quiz_id,
        "user_selection": selection,
        "time_spent_seconds": int(spent),
        "flow_rating": int(rating),
        "fsrs_grade": fsrs_bridge.grade_for_flow(rating),
        "context_mode": context,
        "error_category": category,
        "client_id": client_id,
        "reviewed_at": moment,
    }


def push_reviews(reviews: list[dict], *, path: str | None = None,
                 session_id: str = "default") -> dict:
    """Persist device reviews, update FSRS cards, and advance the flow state."""
    accepted = duplicates = 0
    errors: list[str] = []
    fsrs: list[dict] = []
    momentum: dict | None = None
    connection = db.open_database(path)
    try:
        for index, raw in enumerate(reviews):
            try:
                review = _validate_review(raw)
            except (ValueError, TypeError) as error:
                errors.append(f"review {index}: {error}")
                continue
            question = connection.execute(
                """SELECT id,concept_id,topic_folder,correct_index,status
                   FROM quiz_bank WHERE id=?""", (review["quiz_id"],)).fetchone()
            if not question:
                errors.append(f"review {index}: unknown quiz_id {review['quiz_id']}")
                continue
            correct = int(review["user_selection"] == int(question["correct_index"])) \
                if review["user_selection"] is not None else 0
            cursor = connection.execute(
                """INSERT OR IGNORE INTO flow_review_logs
                (quiz_id,user_selection,is_correct,time_spent_seconds,flow_rating,
                 fsrs_grade,context_mode,error_category,synced_from_client,
                 reviewed_at,client_id)
                VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                (review["quiz_id"], review["user_selection"], correct,
                 review["time_spent_seconds"], review["flow_rating"],
                 review["fsrs_grade"], review["context_mode"],
                 review["error_category"], review["reviewed_at"].isoformat(),
                 review["client_id"]),
            )
            if cursor.rowcount == 0:
                duplicates += 1
                continue
            connection.commit()
            try:
                result = fsrs_bridge.apply_review(
                    str(question["concept_id"]), review["fsrs_grade"],
                    folder=str(question["topic_folder"] or ""),
                    mistake_pattern=str(review["error_category"] or ""),
                    reviewed_at=review["reviewed_at"], path=path)
            except Exception as error:
                # Roll the log back so the device can retry the same client_id:
                # a stored review whose card never advanced would be lost work.
                connection.execute("DELETE FROM flow_review_logs WHERE id=?",
                                   (cursor.lastrowid,))
                connection.commit()
                errors.append(f"review {index}: fsrs update failed ({type(error).__name__})")
                continue
            fsrs.append(result)
            accepted += 1
            try:
                interleaving.resolve_weakness(review["quiz_id"], bool(correct), path=path)
            except Exception:
                # Reinforcement bookkeeping must never lose an accepted review.
                pass
            connection.execute(
                "UPDATE quiz_bank SET status='ANSWERED' WHERE id=? AND status<>'ANSWERED'",
                (review["quiz_id"],))
            connection.commit()
            momentum = flow_scheduler.record_rating(
                session_id, review["flow_rating"],
                context_mode=review["context_mode"], path=path)
    finally:
        connection.close()
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": len(errors),
        "errors": errors[:5],
        "fsrs": fsrs[-5:],
        "session": momentum,
    }


def deck_for_download(limit: int = DEFAULT_DECK_LIMIT, mode: str = "all",
                      path: str | None = None) -> dict:
    """Build a complete deck without changing any status (manual file export)."""
    return pull_deck(limit, mode, path=path, mark=False, include_synced=True)
