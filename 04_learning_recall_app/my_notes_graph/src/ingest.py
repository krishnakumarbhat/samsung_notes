"""Full local corpus indexing with honest processed/searchable coverage."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from src import local_index, memory
from src.config import PROJECT_ROOT


def index_export_report(report_path: Path | None = None, limit: int = 20,
                        uuids: list[str] | None = None) -> dict:
    """Scan all compressed PDFs, hash-skip unchanged ones; ignore stale report limits."""
    # A report of updated=0 must never prevent an initial full local index.
    return local_index.index_all()


def coverage() -> dict:
    """Separate physical PDFs, exact export matches, processed rows, and text-searchable PDFs."""
    result = local_index.coverage()
    original = PROJECT_ROOT / "02_extracted_notes_pdf/notes/original"
    actual = list(original.rglob("*.pdf"))
    state = PROJECT_ROOT / "02_extracted_notes_pdf/ai_agend_history/incremental_state.sqlite"
    tracked = set()
    if state.is_file():
        con = sqlite3.connect(state.as_uri() + "?mode=ro", uri=True)
        con.execute("PRAGMA query_only=ON")
        try:
            tracked = {Path(row[0]).relative_to(original).as_posix() for row in con.execute("SELECT path FROM exports")
                       if row[0] and Path(row[0]).is_file() and Path(row[0]).is_relative_to(original)}
        finally:
            con.close()
    for folder in result["folders"]:
        prefix = folder.get("folder_path", folder["folder"])
        if prefix == "Uncategorized":
            subset = [p for p in actual if p.relative_to(original).as_posix().startswith("Uncategorized/")
                      or len(p.relative_to(original).parts) == 1]
        else:
            subset = [p for p in actual if p.relative_to(original).as_posix().startswith(prefix + "/")]
        folder["pdfs"] = len(subset)
        folder["extracted"] = sum(p.relative_to(original).as_posix() in tracked for p in subset)
        folder["indexed"] = folder["indexed_processed"]
        folder["indexed_pct"] = round(100 * folder["indexed"] / folder["pdfs"]) if folder["pdfs"] else 0
        topics = [topic for topic in memory.stats() if (topic.get("folder") or "Uncategorized").startswith(prefix)]
        folder["concepts"] = len(topics)
        folder["mastered"] = sum(bool(topic["mastered"]) for topic in topics)
        folder["mastery_pct"] = round(100 * folder["mastered"] / len(topics)) if topics else 0
    result.update(total_pdfs=len(actual), exported_pdfs=len(tracked), indexed_sources=result["indexed_processed"],
                  processed_pct=round(100 * result["indexed_processed"] / len(actual)) if actual else 0,
                  explanation="Local PDFs include legacy/untracked exports. Processed includes explicit empty/unreadable status; searchable requires actual text.")
    return result


def ingest_bytes(name: str, data: bytes):
    """Manual legacy upload is disabled to avoid untracked writes outside the corpus."""
    raise ValueError("Place PDFs in notes/original and run the local indexer; raw uploads are not sent to Gemini")
