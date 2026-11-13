"""Micro-batch question factory for the decoupled study bank.

Design rules that matter:

* **Never one giant prompt.**  Each call covers one retrieved chunk and asks for
  4-6 questions, so a failure costs a few hundred tokens, not the whole run.
* **Checkpoint after every batch.**  A rate-limit window never loses generated
  questions because each accepted batch is committed immediately.
* **Validate before storing.**  A question that is not four distinct options,
  does not follow the cognitive matrix, or uses the wrong math delimiters is
  rejected rather than shown to the learner.
* **Coverage first, then weakness.**  Notes with no questions are targeted
  before notes that already have a healthy pool; fragile FSRS concepts are
  targeted before stable ones.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.config import NOTES_DIR

from . import db
from .gemini_batch import BatchError, BatchGemini

COGNITIVE_TYPES = ("INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "SYSTEM_APPLICATION")
MIX = {"INTUITION": 0.30, "CALCULATION_STEP": 0.35,
       "ADVERSARIAL_TRAP": 0.20, "SYSTEM_APPLICATION": 0.15}
# A 20-slot rotation reproduces the requested 30/35/20/15 mix exactly
# (6/7/4/3) and never places the same cognitive type twice in a row, so any
# five consecutive slots form a legal micro-batch.
ROTATION: tuple[str, ...] = (
    "INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "INTUITION",
    "CALCULATION_STEP", "SYSTEM_APPLICATION", "INTUITION", "CALCULATION_STEP",
    "ADVERSARIAL_TRAP", "INTUITION", "CALCULATION_STEP", "SYSTEM_APPLICATION",
    "INTUITION", "CALCULATION_STEP", "ADVERSARIAL_TRAP", "INTUITION",
    "CALCULATION_STEP", "SYSTEM_APPLICATION", "ADVERSARIAL_TRAP",
    "CALCULATION_STEP",
)

SCRATCHPAD_TYPES = {"CALCULATION_STEP"}
ALLOWED_DELIMITERS = re.compile(r"\\\(|\\\)|\\\[|\\\]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class QuizSchemaError(ValueError):
    """A generated batch failed validation and was not written to the bank."""


@dataclass
class Target:
    """One concept cluster the factory can ask about."""

    concept_id: str
    folder: str = ""
    source_pdf: str = ""
    page_num: int = 0
    reason: str = "coverage"


@dataclass
class FactoryReport:
    """Aggregate outcome of one factory run."""

    target_pool: int = 0
    started_bank: int = 0
    batches: int = 0
    inserted: int = 0
    rejected: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    finished_bank: int = 0

    def to_dict(self) -> dict:
        return {
            "target_pool": self.target_pool,
            "started_bank": self.started_bank,
            "batches": self.batches,
            "inserted": self.inserted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "errors": self.errors[-5:],
            "finished_bank": self.finished_bank,
        }


def notes_root() -> Path:
    """Return the tree that holds the searchable derived PDFs."""
    derived = Path(NOTES_DIR) / "compressed"
    return derived if derived.is_dir() else Path(NOTES_DIR)


def note_inventory(root: Path | None = None) -> list[dict]:
    """List every derived note with its folder and size, largest first."""
    base = root or notes_root()
    notes: list[dict] = []
    for path in base.rglob("*.pdf"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.relative_to(base)
        except ValueError:
            relative = Path(path.name)
        notes.append({
            "path": str(path),
            "relative": str(relative),
            "folder": str(relative.parent).replace("\\", "/") if relative.parent.name else "",
            "title": path.stem,
            "bytes": path.stat().st_size,
        })
    notes.sort(key=lambda item: -item["bytes"])
    return notes


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{4,}", text.lower())}


def resolve_source(concept: str, folder: str, inventory: list[dict]) -> dict | None:
    """Pick the note that best supports a concept, preferring the same folder."""
    scoped = [note for note in inventory
              if folder and note["folder"].lower().startswith(folder.lower())] or inventory
    if not scoped:
        return None
    wanted = _tokens(concept)
    best = max(scoped, key=lambda note: (len(wanted & _tokens(note["title"])), note["bytes"]))
    return best


def plan_targets(inventory: list[dict], *, limit: int = 120, path: str | None = None) -> list[Target]:
    """Order concepts by coverage gap, then by memory fragility."""
    connection = db.open_database(path)
    try:
        counts = {row["source_pdf"]: row["n"] for row in connection.execute(
            "SELECT source_pdf, COUNT(*) AS n FROM quiz_bank GROUP BY source_pdf")}
        folder_counts = {row["topic_folder"]: row["n"] for row in connection.execute(
            "SELECT topic_folder, COUNT(*) AS n FROM quiz_bank GROUP BY topic_folder")}
        cards = connection.execute(
            """SELECT topic, folder, COALESCE(stability,0) AS stability
               FROM fsrs_cards ORDER BY COALESCE(stability,0) ASC""").fetchall()
    finally:
        connection.close()

    targets: list[Target] = []
    seen: set[str] = set()
    for card in cards:
        concept = str(card["topic"])
        if not concept or concept in seen:
            continue
        source = resolve_source(concept, str(card["folder"] or ""), inventory)
        targets.append(Target(
            concept_id=concept,
            folder=str(card["folder"] or (source["folder"] if source else "")),
            source_pdf=str(source["relative"] if source else ""),
            reason=f"weak-memory (S={float(card['stability']):.2f})",
        ))
        seen.add(concept)

    uncovered = [note for note in inventory if counts.get(note["relative"], 0) == 0]
    uncovered.sort(key=lambda note: (-note["bytes"], note["relative"]))
    for note in uncovered:
        concept = note["title"]
        if concept in seen:
            continue
        targets.append(Target(concept_id=concept, folder=note["folder"],
                              source_pdf=note["relative"], reason="no-questions-yet"))
        seen.add(concept)

    for folder, count in sorted(folder_counts.items(), key=lambda item: item[1]):
        if count >= 3 or not folder:
            continue
        note = resolve_source(folder.replace("/", " "), folder, inventory)
        if not note or note["title"] in seen:
            continue
        targets.append(Target(concept_id=folder.replace("/", " ") + " core",
                              folder=folder, source_pdf=note["relative"],
                              reason=f"folder has only {count} question(s)"))
        seen.add(note["title"])

    return targets[:max(1, limit)]


def plan_types(count: int, offset: int) -> list[str]:
    """Take the next ``count`` cognitive types from the 20-slot rotation."""
    return [ROTATION[(offset + index) % len(ROTATION)] for index in range(count)]


def dedupe_key(concept_id: str, question: str) -> str:
    """Stable identity of a question so re-runs cannot duplicate it."""
    normal = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    return hashlib.sha256(f"{concept_id}|{normal}".encode()).hexdigest()


def _text(question: dict, field_name: str, minimum: int, maximum: int) -> str:
    value = question.get(field_name)
    if not isinstance(value, str):
        raise QuizSchemaError(f"{field_name} must be a string")
    value = CONTROL.sub(" ", value).strip()
    if not minimum <= len(value) <= maximum:
        raise QuizSchemaError(f"{field_name} length {len(value)} outside {minimum}-{maximum}")
    return value


def check_math(text: str) -> None:
    """Require KaTeX delimiters and reject LaTeX's native bracket delimiters."""
    if ALLOWED_DELIMITERS.search(text):
        raise QuizSchemaError("use $...$/$$...$$ delimiters, not \\( or \\[")
    if text.count("$") % 2:
        raise QuizSchemaError("unbalanced $ delimiters")


def validate_questions(payload: dict, target: Target,
                       expected: list[str] | None = None) -> list[dict]:
    """Validate and normalise one model batch; raise on the first bad question.

    ``expected`` is the planned cognitive sequence.  It is advisory: a model
    that picks a different but legal type keeps it rather than being silently
    rewritten, because the mix is a pool-level budget and a rejected batch
    costs a full retry.
    """
    del expected  # the plan shapes the prompt; drift is accepted if it is legal
    raw = payload.get("questions")
    if not isinstance(raw, list) or not 4 <= len(raw) <= 6:
        raise QuizSchemaError("a batch must contain 4-6 questions")
    cleaned: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise QuizSchemaError(f"question {index} is not an object")
        cognitive = str(item.get("cognitive_type", "")).strip().upper()
        if cognitive not in COGNITIVE_TYPES:
            raise QuizSchemaError(f"question {index} has unknown cognitive_type")
        question_md = _text(item, "question_md", 12, 1400)
        explanation_md = _text(item, "explanation_md", 12, 4000)
        check_math(question_md)
        check_math(explanation_md)
        options = item.get("options")
        if not isinstance(options, list) or len(options) != 4:
            raise QuizSchemaError(f"question {index} must have exactly four options")
        normalised_options = []
        for option in options:
            if not isinstance(option, str):
                raise QuizSchemaError(f"question {index} has a non-text option")
            value = CONTROL.sub(" ", option).strip()
            if not 1 <= len(value) <= 400:
                raise QuizSchemaError(f"question {index} has an empty or oversized option")
            check_math(value)
            normalised_options.append(value)
        if len({option.lower() for option in normalised_options}) != 4:
            raise QuizSchemaError(f"question {index} has duplicate options")
        correct = item.get("correct_index")
        if not isinstance(correct, int) or not 0 <= correct <= 3:
            raise QuizSchemaError(f"question {index} has an invalid correct_index")
        tier = item.get("difficulty_tier", 3)
        if isinstance(tier, str) and tier.isdigit():
            tier = int(tier)
        if not isinstance(tier, int) or not 1 <= tier <= 5:
            raise QuizSchemaError(f"question {index} has an invalid difficulty_tier")
        scratchpad = 1 if cognitive in SCRATCHPAD_TYPES else 0
        key = dedupe_key(target.concept_id, question_md)
        if key in seen:
            raise QuizSchemaError(f"question {index} repeats another in the batch")
        seen.add(key)
        cleaned.append({
            "concept_id": target.concept_id,
            "topic_folder": target.folder,
            "source_pdf": target.source_pdf,
            "page_num": int(target.page_num),
            "cognitive_type": cognitive,
            "requires_scratchpad": scratchpad,
            "difficulty_tier": tier,
            "question_md": question_md,
            "options_json": json.dumps(normalised_options, ensure_ascii=False),
            "correct_index": correct,
            "explanation_md": explanation_md,
            "dedupe_key": key,
        })
    if not any(item["requires_scratchpad"] == 0 for item in cleaned):
        raise QuizSchemaError("a batch needs at least one desk-free question")
    return cleaned


SYSTEM_PROMPT = """You are a study-question author for a spaced-repetition tutor.
Return only JSON. Every question must be answerable from the supplied note excerpt.
Use KaTeX math delimiters: $for inline$ and $$for display$$. Never use \\( \\) or \\[ \\].
Exactly four options, all distinct, exactly one correct. The explanation must show the
step-by-step derivation, the intuition, and any shortcut trick."""


def build_prompt(target: Target, chunks: list[tuple[str, str]], types: list[str]) -> str:
    """Compose one chunk-scoped prompt asking for exactly the planned types."""
    context = "\n\n".join(
        f"[excerpt {index} from {source}]\n{text[:1400]}"
        for index, (source, text) in enumerate(chunks, 1)
    )
    scratch_rule = {
        "INTUITION": "plain-English mental model, no pencil arithmetic, requires mental reasoning only",
        "CALCULATION_STEP": "multi-step derivation the learner must work on paper",
        "ADVERSARIAL_TRAP": "a flawed step, edge case, or violated assumption to spot",
        "SYSTEM_APPLICATION": "map the idea onto code, caches, robotics, or hardware constraints",
    }
    plan = "\n".join(f"{index + 1}. {name} — {scratch_rule[name]}" for index, name in enumerate(types))
    return f"""Concept: {target.concept_id}
Folder: {target.folder or 'unscoped'}
Source note: {target.source_pdf or 'n/a'}

Write exactly {len(types)} questions, one per planned cognitive type:
{plan}

Return this JSON shape:
{{"questions":[{{
  "cognitive_type":"INTUITION",
  "difficulty_tier":3,
  "question_md":"...",
  "options":["...","...","...","..."],
  "correct_index":0,
  "explanation_md":"..."
}}]}}

Rules:
- Write exactly the planned questions, in the same order, one per line of the plan.
- 4 to 6 questions total.
- difficulty_tier is 1 (recall) to 5 (exam-hard).
- The correct answer must not always be first.
- Question text must stand alone: no "according to the excerpt".

Note excerpts:
{context}
"""


def store_chunks(target: Target, *, k: int = 3) -> list[tuple[str, str]]:
    """Retrieve the top chunks for a target from the offline local index.

    ``local_index`` is rebuilt from the compressed corpus and needs no API
    call, so generation stays grounded even on a metered connection.  The
    legacy Gemini-embedded store is only a fallback for an index that has not
    been built yet.
    """
    query = target.concept_id.replace("_", " ").replace("/", " ")
    try:
        from src import local_index

        found = local_index.search(query, k=k, folder=target.folder or None)
        if found:
            return [(str(item["source"]), str(item["text"])) for item in found]
    except Exception:
        pass
    from src import store

    try:
        if target.folder:
            found = store.search(query, k=k, folder_filter=target.folder)
            if found:
                return found
        return store.search(query, k=k, source_filter=Path(target.source_pdf).name) or \
            store.search(query, k=k)
    except Exception:
        return []


class QuizFactory:
    """Autonomous batch worker: target, retrieve, generate, validate, checkpoint."""

    def __init__(self, *, path: str | None = None, client: BatchGemini | None = None,
                 inventory: list[dict] | None = None, chunk_provider=None,
                 pause: float = 1.5, sleep=time.sleep):
        self.path = path
        self.client = client or BatchGemini()
        self.inventory = inventory if inventory is not None else note_inventory()
        self.chunk_provider = chunk_provider or store_chunks
        self.pause = max(0.0, float(pause))
        self.sleep = sleep

    def bank_size(self) -> int:
        connection = db.open_database(self.path)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM quiz_bank").fetchone()[0])
        finally:
            connection.close()

    def commit(self, questions: list[dict], batch_id: str) -> tuple[int, int]:
        """Insert a validated batch; return ``(inserted, duplicates)``."""
        connection = db.open_database(self.path)
        inserted = duplicates = 0
        try:
            for question in questions:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO quiz_bank
                    (concept_id,topic_folder,source_pdf,page_num,cognitive_type,
                     requires_scratchpad,difficulty_tier,question_md,options_json,
                     correct_index,explanation_md,dedupe_key,batch_id)
                    VALUES(:concept_id,:topic_folder,:source_pdf,:page_num,:cognitive_type,
                           :requires_scratchpad,:difficulty_tier,:question_md,:options_json,
                           :correct_index,:explanation_md,:dedupe_key,:batch_id)""",
                    {**question, "batch_id": batch_id},
                )
                inserted += cursor.rowcount
                duplicates += 1 - cursor.rowcount
            connection.commit()
        finally:
            connection.close()
        return inserted, duplicates

    def run(self, target_pool: int, *, per_batch: int = 5, max_batches: int | None = None,
            dry_run: bool = False, verbose: bool = True) -> FactoryReport:
        """Generate until the bank holds ``target_pool`` questions or work runs out."""
        report = FactoryReport(target_pool=target_pool, started_bank=self.bank_size())
        targets = plan_targets(self.inventory, path=self.path)
        if not targets:
            report.errors.append("no target concepts: index notes first")
            return report
        pause = self.pause

        def log(message: str) -> None:
            if verbose:
                print(message, flush=True)

        cursor = 0
        offset = 0
        while report.started_bank + report.inserted < target_pool:
            if max_batches is not None and report.batches >= max_batches:
                break
            if cursor >= len(targets) * 3:
                report.errors.append("target list exhausted before the pool target")
                break
            target = targets[cursor % len(targets)]
            cursor += 1
            chunks = self.chunk_provider(target)
            if not chunks:
                continue
            types = plan_types(per_batch, offset)
            offset += per_batch
            prompt = build_prompt(target, chunks, types)
            batch_id = f"{int(time.time())}-{cursor}"
            report.batches += 1
            log(f"[batch {report.batches}] {target.concept_id} "
                f"({target.reason}) -> {len(types)} questions")
            if dry_run:
                log("  dry-run: prompt built, model not called")
                report.inserted += len(types)
                continue
            try:
                payload = self.client.json(prompt, system=SYSTEM_PROMPT, max_output_tokens=5000)
                questions = validate_questions(payload, target, types)
            except (BatchError, QuizSchemaError) as error:
                report.rejected += 1
                report.errors.append(f"{target.concept_id}: {error}")
                log(f"  rejected: {error}")
                if isinstance(error, BatchError):
                    self.sleep(min(30.0, 3.0 * report.rejected))
                continue
            inserted, duplicates = self.commit(questions, batch_id)
            report.inserted += inserted
            report.duplicates += duplicates
            log(f"  committed {inserted} (duplicates {duplicates})")
            if pause:
                self.sleep(pause)
        report.finished_bank = self.bank_size()
        return report


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python3 -m engine.quiz_factory --target-pool 500``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-pool", type=int, default=300,
                        help="stop once the bank holds this many questions")
    parser.add_argument("--per-batch", type=int, default=5, choices=range(4, 7))
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--pause", type=float, default=1.5, help="seconds between model calls")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--path", help="alternate recall database (default data/recall.sqlite)")
    args = parser.parse_args(argv)
    factory = QuizFactory(path=args.path, pause=args.pause)
    report = factory.run(args.target_pool, per_batch=args.per_batch,
                         max_batches=args.max_batches, dry_run=args.dry_run)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.errors == [] or report.inserted else 1


if __name__ == "__main__":
    raise SystemExit(main())
