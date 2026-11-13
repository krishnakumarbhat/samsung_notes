"""Socratic tutor backed by compact hybrid retrieval and FSRS-v5 review."""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

from src import gemini, memory, review as srs, store
from src.config import SVG_DIR, VAULT

SYSTEM = (
    "You are a concise Socratic tutor. Ground claims in the supplied notes when relevant; "
    "say when context is insufficient. Never expose or repeat long note passages. "
    "Use compact answers and return the requested JSON only."
)
GRADE_NAMES = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


def _save_svg(answer: str, topic: str) -> tuple[str, str]:
    """Persist one optional SVG diagram returned by the tutor."""
    match = re.search(r"<svg[\s\S]*?</svg>", answer)
    if not match:
        return answer, ""
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_") or "diagram"
    path = SVG_DIR / f"{safe}_{int(time.time())}.svg"
    path.write_text(match.group(0), encoding="utf-8")
    return (answer[: match.start()] + answer[match.end() :]).strip(), f"/vault/svg/{path.name}"


def _append_vault(entry: str) -> None:
    """Append a short interaction record to the local daily learning vault."""
    VAULT.mkdir(parents=True, exist_ok=True)
    with (VAULT / f"session_{date.today().isoformat()}.md").open("a", encoding="utf-8") as stream:
        stream.write(entry + "\n\n")


def _related_prerequisites(question: str) -> list[str]:
    """Find lexical syllabus matches and return their prerequisite terms."""
    query_tokens = set(store.tokenize(question))
    syllabus = memory.get_syllabus() or []
    scored = []
    for concept in syllabus:
        name = str(concept.get("name", ""))
        overlap = len(query_tokens.intersection(store.tokenize(name)))
        if overlap:
            scored.append((overlap, concept))
    scored.sort(key=lambda item: (-item[0], item[1].get("name", "")))
    prerequisites: list[str] = []
    for _, concept in scored[:2]:
        prerequisites.extend(str(value) for value in concept.get("prereq", []))
    return list(dict.fromkeys(prerequisites))[:6]


def _retrieved_context(
    question: str,
    max_chars: int = 3000,
    source_filter: str | None = None,
    folder_filter: str | None = None,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Retrieve a bounded context and compact page citations."""
    from src import local_index
    hits = local_index.search(question, k=3, max_chars=max_chars,
                              sources=[source_filter] if source_filter else None,
                              folder=folder_filter)
    snippets = [(hit["source"], f"## Page {hit['page']}\n{hit['text']}") for hit in hits]
    citations = [{"source": hit["source"], "page": hit["page"]} for hit in hits]
    return snippets, citations


def _bounded_context(snippets: list[tuple[str, str]], char_budget: int = 3000) -> str:
    """Pack only retrieved chunks into a deterministic short context block."""
    blocks = []
    remaining = char_budget
    for source, text in snippets:
        if remaining <= 0:
            break
        block = f"[{source}] {text[:remaining]}"
        blocks.append(block)
        remaining -= len(block) + 8
    return "\n---\n".join(blocks) if blocks else "(no indexed notes found)"


def ensure_syllabus() -> list[dict]:
    """Generate a small concept prerequisite graph once, with a bounded filename-only prompt."""
    syllabus = memory.get_syllabus()
    if syllabus:
        enriched = []
        for item in syllabus:
            current = dict(item)
            if not current.get("folder"):
                current["folder"] = _infer_folder(str(current.get("name", "")))
            enriched.append(current)
        if enriched != syllabus:
            memory.set_syllabus(enriched)
        return enriched
    sources = store.sources()
    names = [str(item["source"])[:100] for item in sources[:35]]
    if not names:
        return []
    prompt = (
        "Create a concise learning path from these document labels only. "
        "Return JSON with topics [{name,folder,prereq[]}], 10-16 topics. "
        "Do not infer private content not present in the labels.\n"
        + "\n".join(f"- {name}" for name in names)
    )
    try:
        generated = gemini.generate_json(prompt, system=SYSTEM, max_output_tokens=500)
        topics = generated.get("topics", [])[:20]
        clean = [
            {
                "name": str(item.get("name", "")).strip()[:100],
                "folder": (str(item.get("folder", "")).strip() or _infer_folder(str(item.get("name", ""))))[:100],
                "prereq": [str(p)[:100] for p in item.get("prereq", [])[:8]],
            }
            for item in topics
            if str(item.get("name", "")).strip()
        ]
    except Exception as error:  # noqa: BLE001
        print(f"[tutor] syllabus deferred ({type(error).__name__})")
        clean = []
    if clean:
        memory.set_syllabus(clean)
    return clean


def _infer_folder(topic: str) -> str:
    """Map existing topic names to the closest local note root without an API call."""
    value = topic.casefold()
    if any(term in value for term in ("robot", "kinematic", "control system")):
        return "Mlaidlgenai_robo/Robotics"
    if any(term in value for term in ("math", "algebra", "calculus", "geometry", "probability", "theorem", "derivative", "integral")):
        return "maths"
    if any(term in value for term in ("machine learning", "deep learning", "neural", "llm", "language model", "ai", "algorithm", "data structure")):
        return "Mlaidlgenai_robo"
    if any(term in value for term in ("psychology", "history", "journal", "reflection", "philosophy")):
        return "Thoughts"
    return "Uncategorized"


def ask(question: str) -> dict:
    """Answer a question from top-k retrieved chunks under the configured token budget."""
    question = question.strip()
    if not question:
        raise ValueError("Question cannot be empty.")
    if len(question) > 1200:
        raise ValueError("Please keep the question under 1,200 characters.")
    snippets, citations = _retrieved_context(question, max_chars=2600)
    profile = memory.learner_profile(max_topics=3)[:400]
    prompt = (
        f"Learner state: {profile}\n\n"
        f"Retrieved note chunks (use only the parts needed):\n{_bounded_context(snippets, 2600)}\n\n"
        f"Question: {question}\n\n"
        "Answer concisely, explain the key reasoning, and identify one source page if available."
    )
    answer = gemini.generate(prompt, system=SYSTEM, max_output_tokens=600)
    clean, svg_url = _save_svg(answer, question[:60])
    memory.log_qa(
        "asked", question[:80], question, clean[:1800],
        source=citations[0]["source"] if citations else "",
        source_page=citations[0]["page"] if citations else None,
    )
    _append_vault(f"### Q: {question[:250]}\n\n{clean[:1200]}")
    return {"answer": clean, "svg": svg_url, "sources": citations}


def _select_mode(topic: str, folder: str, mistake: str) -> str:
    """Choose a question angle from folder, topic, and the latest mistake."""
    if mistake:
        return "Bug Diagnosis" if any(x in mistake.lower() for x in ("syntax", "edge", "bug")) else "Adversarial Edge-Case"
    area = f"{folder} {topic}".casefold()
    if "math" in area or any(x in area for x in ("calculus", "algebra", "theorem", "proof")):
        return "First-Principles Derivation"
    if any(x in area for x in ("code", "algorithm", "dsa", "programming", "implementation")):
        return "Code Implementation"
    return "Adversarial Edge-Case"


def probe(scope: str = "Daily Adaptive Mix") -> dict:
    """Generate one open-ended Socratic question with private grading points."""
    syllabus = ensure_syllabus()
    source_filter = scope[5:] if scope.startswith("note:") else None
    folder_filter = scope[7:] if scope.startswith("folder:") else None
    picked = memory.next_probe_topic()
    topic, is_review = picked if picked else (scope or "foundational concepts", False)
    if source_filter:
        topic = Path(source_filter).stem
        is_review = False
    elif scope and scope not in ("Daily Adaptive Mix", "Daily Adaptive Mix / All folders"):
        selected_folder = folder_filter or scope
        scoped = [
            row for row in memory.stats()
            if row.get("folder", "").startswith(selected_folder)
            or row["topic"].casefold() in selected_folder.casefold()
        ]
        if scoped:
            scoped.sort(key=lambda row: (not row["total"], row["pct"], row["topic"]))
            topic = scoped[0]["topic"]
    concept = next((row for row in syllabus if row.get("name") == topic), {})
    folder = folder_filter or (
        Path(source_filter).parent.as_posix()
        if source_filter and Path(source_filter).parent.as_posix() != "."
        else str(concept.get("folder", ""))
    )
    mistake = memory.latest_mistake(topic)
    mode = _select_mode(topic, folder, mistake)
    question_seed = f"{topic} {folder} {mode}"
    snippets, citations = _retrieved_context(
        question_seed,
        max_chars=2200,
        source_filter=source_filter,
        folder_filter=folder_filter,
    )
    context = _bounded_context(snippets, 2200)
    prompt = (
        f"Create ONE open-response Socratic quiz question about {topic}.\n"
        f"Angle: {mode}. Prior mistake pattern to target: {mistake or 'none recorded'}.\n"
        f"Relevant notes:\n{context}\n\n"
        "Return JSON only with fields question (one concise prompt), expected_points "
        "(2-4 short rubric bullets), and hint (one short, non-answer hint). "
        "Do not return multiple choice options or the final answer."
    )
    try:
        generated = gemini.generate_json(prompt, system=SYSTEM, temperature=0.45, max_output_tokens=450)
        question = str(generated.get("question", "")).strip()[:700]
        expected = [str(point)[:180] for point in generated.get("expected_points", [])[:4]]
        hint = str(generated.get("hint", "")).strip()[:250]
    except Exception as error:  # noqa: BLE001
        print(f"[tutor] generation fallback ({type(error).__name__})")
        question = f"Explain {topic} from first principles. State your assumptions and one way to test your reasoning."
        expected = [f"A sound definition of {topic}", "A correct reasoning chain", "A check or example"]
        hint = "Start by naming the smallest principle this idea depends on."
    rubric = json.dumps({"expected_points": expected, "hint": hint}, ensure_ascii=False)
    question_id = memory.log_qa(
        "probed", topic, question, "", rubric=rubric, mode=mode,
        source=citations[0]["source"] if citations else "",
        source_page=citations[0]["page"] if citations else None,
    )
    return {
        "id": question_id,
        "topic": topic,
        "mode": mode,
        "question": question,
        "hint": hint,
        "source": citations[0] if citations else None,
        "is_review": is_review,
    }


def evaluate_socratic_answer(question_id: int, answer: str) -> dict:
    """Rubric-grade a free response and persist it before the learner rates recall."""
    answer = answer.strip()
    if not answer:
        raise ValueError("Please enter an answer before grading.")
    if len(answer) > 2000:
        raise ValueError("Please keep the answer under 2,000 characters.")
    probe_record = memory.get_qa(question_id)
    if not probe_record or probe_record["direction"] != "probed":
        raise ValueError("Quiz question expired; request a new question.")
    topic = str(probe_record["topic"])
    rubric = json.loads(probe_record.get("rubric") or "{}")
    expected = rubric.get("expected_points", [])
    prompt = (
        f"Topic: {topic}\nQuestion: {probe_record['question']}\n"
        f"Rubric points: {json.dumps(expected, ensure_ascii=False)}\n"
        f"Learner answer: {answer}\n\n"
        "Grade the answer using FSRS ratings: 1=Again (missing/incorrect), "
        "2=Hard (partially correct, substantial prompting), 3=Good (correct with minor gaps), "
        "4=Easy (correct, complete, precise). Return JSON only: grade (integer 1-4), "
        "feedback (brief actionable explanation), mistake_pattern (short category, or empty string)."
    )
    try:
        outcome = gemini.generate_json(prompt, system=SYSTEM, temperature=0.2, max_output_tokens=450)
        grade = int(outcome.get("grade", 1))
        if grade not in (1, 2, 3, 4):
            raise ValueError("rubric returned an invalid FSRS grade")
        feedback = str(outcome.get("feedback", "")).strip()[:1200]
        mistake = str(outcome.get("mistake_pattern", "")).strip()[:100]
        grader = "gemini"
    except Exception as error:  # noqa: BLE001
        grade, feedback, mistake, grader = _local_grade(answer, expected), "Local fallback rubric used; review the expected concepts.", "", "local-fallback"
        print(f"[tutor] grading fallback ({type(error).__name__})")
    memory.update_qa_result(question_id, answer, grade, mistake, feedback[:1200])
    return {
        "answer_id": question_id,
        "topic": topic,
        "grade": grade,
        "grade_name": GRADE_NAMES[grade],
        "feedback": feedback,
        "mistake_pattern": mistake,
        "grader": grader,
    }


def apply_fsrs_rating(question_id: int, rating: int) -> dict:
    """Persist the learner's Again/Hard/Good/Easy choice and schedule FSRS-v5."""
    if int(rating) not in (1, 2, 3, 4):
        raise ValueError("rating must be in the range 1-4")
    probe_record = memory.get_qa(question_id)
    if not probe_record or not probe_record.get("answer"):
        raise ValueError("No graded answer is available for this question.")
    topic = str(probe_record["topic"])
    source = str(probe_record.get("source") or "")
    folder = source.rsplit("/", 1)[0] if "/" in source else ""
    rating = int(rating)
    mistake = str(probe_record.get("mistake_pattern") or "")
    memory.update_qa_result(
        question_id, str(probe_record["answer"]), rating, mistake,
        str(probe_record.get("feedback") or ""),
    )
    memory.record_answer(topic, rating >= 3, folder=folder)
    schedule = srs.update(topic, rating, folder=folder, mistake_pattern=mistake)
    _append_vault(f"### Quiz: {topic} ({GRADE_NAMES[rating]})\n\n{probe_record['answer'][:700]}")
    return {
        "topic": topic,
        "grade": rating,
        "grade_name": GRADE_NAMES[rating],
        "next_review_days": schedule["next_review_days"],
        "retrievability": schedule["retrievability"],
    }


def grade_socratic(question_id: int, answer: str) -> dict:
    """Compatibility endpoint that accepts the model's suggested rating."""
    outcome = evaluate_socratic_answer(question_id, answer)
    outcome.update(apply_fsrs_rating(question_id, outcome["grade"]))
    return outcome


def _local_grade(answer: str, expected_points: list[str]) -> int:
    """Small offline fallback based on expected-concept keyword coverage."""
    answer_terms = set(store.tokenize(answer))
    expected_terms = [set(store.tokenize(point)) for point in expected_points if point]
    if not answer_terms or not expected_terms:
        return 1
    coverage = sum(bool(answer_terms & terms) for terms in expected_terms) / len(expected_terms)
    if coverage >= 0.9 and len(answer_terms) >= 16:
        return 4
    if coverage >= 0.65:
        return 3
    if coverage >= 0.25:
        return 2
    return 1


def upload(name: str, markdown: str, n_chunks: int) -> dict:
    """Return current syllabus; generation remains on-demand, not automatic at startup."""
    topics = memory.get_syllabus() or []
    return {"topics": topics, "uploaded": name, "chunks": n_chunks}
