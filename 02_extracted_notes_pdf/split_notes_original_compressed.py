"""Idempotent, guarded original/derived migration; never copy or remove PDF data."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "02_extracted_notes_pdf/notes"
HISTORY = ROOT / "02_extracted_notes_pdf/ai_agend_history"
STATE = HISTORY / "incremental_state.sqlite"


def main() -> None:
    """Restore exact UUID/path associations from the pre-split backup and fix placeholders."""
    if shutil.disk_usage(ROOT).free < 15 * 1024**3:
        raise RuntimeError("Free space is below 15 GiB")
    original, compressed = NOTES / "original", NOTES / "compressed"
    original.mkdir(exist_ok=True)
    compressed.mkdir(exist_ok=True)
    for entry in NOTES.iterdir():
        if entry.name in {"original", "compressed", "export_manifest.json"}:
            continue
        target = original / entry.name
        if not target.exists():
            entry.rename(target)
    baselines = sorted(HISTORY.glob("incremental_state.pre_split.*.sqlite"))
    if not baselines:
        raise RuntimeError("Exact pre-split baseline missing; refuse heuristic UUID remapping")
    baseline = sqlite3.connect(baselines[0].as_uri() + "?mode=ro", uri=True)
    baseline.execute("PRAGMA query_only=ON")
    mappings = []
    for uuid, old in baseline.execute("SELECT uuid,path FROM exports"):
        if "/02_extracted_notes_pdf/notes/" in old:
            relative = old.split("/02_extracted_notes_pdf/notes/", 1)[1]
        elif "/samsung_notes/notes/" in old:
            relative = old.split("/samsung_notes/notes/", 1)[1]
        else:
            raise ValueError(f"Unrecognized baseline path for {uuid}")
        target = original / relative
        if not target.is_file() or not target.resolve().is_relative_to(original.resolve()):
            raise ValueError(f"Baseline export unavailable: {uuid}")
        mappings.append((str(target), uuid))
    baseline.close()
    connection = sqlite3.connect(STATE)
    backup_path = HISTORY / f"incremental_state.pre_stage2.{time.time_ns()}.sqlite"
    with sqlite3.connect(backup_path) as backup:
        connection.backup(backup)
    with connection:
        connection.executemany("UPDATE exports SET path=? WHERE uuid=?", mappings)
    connection.close()
    fixed = 0
    for path in original.rglob("*.pdf"):
        relative = path.relative_to(original)
        link = compressed / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()  # replace only a link, never a derived PDF
        elif link.exists():
            continue
        link.symlink_to(os.path.relpath(path, link.parent))
        fixed += 1
    # Legacy external mirror locations all use this canonical directory.
    mirror = ROOT / "04_learning_recall_app/notes_mirror"
    mirror.mkdir(exist_ok=True)
    for path, uuid in mappings:
        derived = compressed / Path(path).relative_to(original)
        link = mirror / f"{uuid}.pdf"
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise ValueError(f"Refusing to overwrite a real mirror file: {uuid}")
        link.symlink_to(os.path.relpath(derived, mirror))
    report = {"exact_state_mappings": len(mappings), "relative_placeholders": fixed,
              "state_backup": str(backup_path), "pdfs_deleted": 0,
              "broken_placeholders": sum(not p.is_file() for p in compressed.rglob("*.pdf"))}
    (HISTORY / "stage2_migration.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
