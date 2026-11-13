"""Rebuild the derived PDFs whose structure no longer matches their original.

A derived file must be a faithful, searchable, compressed copy of its
original.  When a note was re-exported, moved, or repaired, an older derived
file can keep an old page count or an old appearance.  This pass rebuilds
those files from the original: the OCR text layer is added where it is
missing, the page images are re-encoded at their exact pixel size, and the
result is verified against the original before it replaces the file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
NOTES = ROOT / "notes"
ORIGINAL = NOTES / "original"
COMPRESSED = NOTES / "compressed"
sys.path.insert(0, str(ROOT / "script"))
sys.path.insert(0, str(PROJECT_ROOT / "03_rtx_compression_ocr"))

from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

import pikepdf  # noqa: E402

REPORT = PROJECT_ROOT / "00_orchestrator/REDERIVE_REPORT.json"


def _structure_matches(original: Path, derived: Path) -> bool:
    try:
        with pikepdf.open(original) as before, pikepdf.open(derived) as after:
            if len(before.pages) != len(after.pages):
                return False
            for old, new in zip(before.pages, after.pages):
                if [float(v) for v in old.mediabox] != [float(v) for v in new.mediabox]:
                    return False
    except Exception:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    os.environ.update(__import__("incremental_export").ocr_env())
    from derive_corpus import derive_file

    results = []
    for derived in sorted(COMPRESSED.rglob("*.pdf")):
        if derived.is_symlink() or not derived.is_file():
            continue
        original = ORIGINAL / derived.relative_to(COMPRESSED)
        if not original.is_file():
            continue
        if _structure_matches(original, derived):
            continue
        entry = {"derived": str(derived), "original": str(original),
                 "before_bytes": derived.stat().st_size}
        try:
            outcome = derive_file(original, ocr=True)
            entry.update({key: value for key, value in outcome.items()
                          if key not in {"warnings"}})
            entry["status"] = "rederived" if args.apply else "dry-run"
        except Exception as exc:
            entry["status"] = "failed"
            entry["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        results.append(entry)
        print(f"{entry['status']:>10} {derived.name[:60]}", flush=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "applied": args.apply,
        "rederived": len(results),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2))
    print(json.dumps({"rederived": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
