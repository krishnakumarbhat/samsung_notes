#!/usr/bin/env python3
"""Backfill invisible searchable text into every sparse page of the corpus.

The audit report lists the PDFs and pages that hold fewer than 50 selectable
characters.  For each such PDF this runner:

1. calls the existing PaddleOCR pipeline (GPU by default) into a staging folder;
2. verifies the staged copy opens, keeps the page count, and does not lose text;
3. atomically replaces the corpus file and deletes the staged copy.

Only one PDF is staged at a time, so the corpus never doubles on disk.

Usage:
    python3 backfill_searchable_text.py --limit 1 --only Researcher
    python3 backfill_searchable_text.py --device gpu --resume
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

import pymupdf  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CORPUS = PROJECT_ROOT / "02_extracted_notes_pdf" / "notes" / "compressed"
REPORT = SCRIPT_DIR / "ocr_coverage_report.json"
STATUS = SCRIPT_DIR / "ocr_backfill_status.json"
STAGING = Path(os.environ.get("SAM_OCR_STAGING", str(Path(tempfile.gettempdir()) / "sam_win_ocr")))
RUNNER = SCRIPT_DIR / "run_ocr.sh"


def text_chars(path: Path) -> tuple[int, int, list[int]]:
    """Return total pages, total non-whitespace characters, and sparse page numbers."""
    document = pymupdf.open(path)
    sparse: list[int] = []
    total = 0
    try:
        pages = document.page_count
        for index in range(pages):
            try:
                text = document.load_page(index).get_text("text")
            except Exception:
                sparse.append(index + 1)
                continue
            count = len("".join((text or "").split()))
            total += count
            if count < 50:
                sparse.append(index + 1)
    finally:
        document.close()
    return pages, total, sparse


def load_status() -> dict:
    if STATUS.exists():
        try:
            return json.loads(STATUS.read_text())
        except ValueError:
            pass
    return {"updated_at": "", "done": {}, "failed": {}}


def save_status(status: dict) -> None:
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2))
    os.replace(temporary, STATUS)


def candidates() -> list[dict]:
    report = json.loads(REPORT.read_text())
    rows = [record for record in report["files"] if record["sparse_pages"]]
    rows.sort(key=lambda record: (record["bytes"], record["path"]))
    return rows


def verify(staged: Path, original: Path) -> tuple[bool, str]:
    """Check the staged PDF before it may replace the original."""
    try:
        pages, chars, _sparse = text_chars(staged)
        old_pages, old_chars, _old_sparse = text_chars(original)
    except Exception as error:
        return False, f"unreadable ({type(error).__name__})"
    if pages != old_pages:
        return False, f"page count changed {old_pages}->{pages}"
    if chars < old_chars:
        return False, f"text shrank {old_chars}->{chars}"
    if chars == old_chars:
        return True, "no new text (blank or featureless pages)"
    return True, f"text {old_chars}->{chars}"


def run_one(record: dict, device: str, status: dict, *, keep_staging: bool) -> dict:
    original = CORPUS / record["path"]
    staged_dir = STAGING / Path(record["path"]).parent
    staged = staged_dir / original.name
    staged_dir.mkdir(parents=True, exist_ok=True)
    if staged.exists():
        staged.unlink()
    started = time.time()
    command = [str(RUNNER), str(original), "--device", device,
               "--output-dir", str(staged_dir)]
    print(f"[ocr] {record['path']} ({len(record['sparse_pages'])} sparse page(s))", flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not staged.exists():
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        return {"ok": False, "error": "pipeline failed: " + " | ".join(tail),
                "seconds": round(time.time() - started, 1)}
    ok, detail = verify(staged, original)
    if not ok:
        staged.unlink(missing_ok=True)
        return {"ok": False, "error": detail, "seconds": round(time.time() - started, 1)}
    backup = original.with_suffix(original.suffix + ".preocr")
    shutil.copy2(original, backup)
    try:
        os.replace(staged, original)
    except Exception as error:
        shutil.copy2(backup, original)
        return {"ok": False, "error": f"replace failed ({type(error).__name__})",
                "seconds": round(time.time() - started, 1)}
    backup.unlink(missing_ok=True)
    if not keep_staging:
        shutil.rmtree(staged_dir, ignore_errors=True)
    return {"ok": True, "detail": detail, "seconds": round(time.time() - started, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=["auto", "cpu", "gpu"], default="gpu")
    parser.add_argument("--limit", type=int, default=0, help="stop after N PDFs (0 = all)")
    parser.add_argument("--only", default="", help="substring filter on the corpus path")
    parser.add_argument("--resume", action="store_true", help="skip files already done")
    parser.add_argument("--refresh", action="store_true",
                        help="reprocess candidates even when the status file marks them done")
    parser.add_argument("--keep-staging", action="store_true")
    args = parser.parse_args()

    status = load_status()
    rows = candidates()
    if args.only:
        rows = [row for row in rows if args.only.lower() in row["path"].lower()]
    processed = 0
    for record in rows:
        if args.resume and not args.refresh and record["path"] in status["done"]:
            continue
        if args.limit and processed >= args.limit:
            break
        outcome = run_one(record, args.device, status, keep_staging=args.keep_staging)
        key = record["path"]
        if outcome["ok"]:
            status["done"][key] = outcome
            status["failed"].pop(key, None)
        else:
            status["failed"][key] = outcome
        save_status(status)
        print(f"[ocr] {'ok' if outcome['ok'] else 'FAIL'}: {outcome.get('detail') or outcome.get('error')} "
              f"({outcome['seconds']}s)", flush=True)
        processed += 1
    print(json.dumps({"done": len(status["done"]), "failed": len(status["failed"])}, indent=2))
    return 0 if not status["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
