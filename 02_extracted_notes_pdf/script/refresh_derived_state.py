"""Refresh the derived-file columns of the local export state.

The state database records, per note, where its derived (searchable, image
compressed) PDF lives.  After a re-derivation the files change, so the
columns are refreshed from the tree itself: the path, the digest, and the
number of selectable characters.  The interview mirror is refreshed as well.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "script"))

import incremental_export as ie  # noqa: E402

HIST = ROOT / "ai_agend_history"
STATE = HIST / "incremental_state.sqlite"
DEST = ROOT / "notes/original"
DERIVED = ROOT / "notes/compressed"


def _digest(path: Path) -> str:
    import hashlib

    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _chars(path: Path) -> int:
    import fitz

    total = 0
    with fitz.open(path) as pdf:
        for page in pdf:
            total += len(page.get_text().strip())
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-mirror", action="store_true")
    args = parser.parse_args(argv)
    state = ie.open_state()
    columns = {row[1] for row in state.execute("PRAGMA table_info(exports)")}
    for name, kind in (("derived_path", "TEXT"), ("derived_sha256", "TEXT"),
                       ("derived_chars", "INTEGER")):
        if name not in columns:
            state.execute(f"ALTER TABLE exports ADD COLUMN {name} {kind}")
    state.commit()
    updated = 0
    missing = 0
    for row in state.execute("SELECT uuid, path FROM exports").fetchall():
        source = Path(row[1]) if row[1] else None
        if not source or not source.is_file():
            missing += 1
            continue
        try:
            relative = source.resolve().relative_to(DEST.resolve())
        except ValueError:
            continue
        derived = DERIVED / relative
        if not derived.exists():
            state.execute(
                "UPDATE exports SET derived_path=NULL, derived_sha256=NULL, derived_chars=NULL "
                "WHERE uuid=?", (row[0],))
            missing += 1
            continue
        state.execute(
            "UPDATE exports SET derived_path=?, derived_sha256=?, derived_chars=? WHERE uuid=?",
            (str(derived), _digest(derived), _chars(derived), row[0]))
        updated += 1
    state.commit()
    if not args.no_mirror:
        ie.refresh_mirror(state)
    state.close()
    print(json.dumps({"updated": updated, "missing": missing}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
