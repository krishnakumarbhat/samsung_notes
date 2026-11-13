#!/usr/bin/env python3
"""Audit per-page selectable-text coverage across the compressed note corpus.

A page is considered searchable when its extracted text holds at least
``--min-chars`` non-whitespace characters.  Pages below the threshold are the
ones the OCR backfill must rewrite with an invisible text sandwich.

Usage:
    python3 audit_pdf_text_coverage.py
    python3 audit_pdf_text_coverage.py --json /tmp/coverage.json --min-chars 50
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

import pymupdf  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT_ROOT / "02_extracted_notes_pdf" / "notes" / "compressed"


def audit_pdf(path: Path, min_chars: int) -> dict:
    """Return page-level text coverage for one PDF, never raising on damage."""
    record = {
        "path": str(path.relative_to(DEFAULT_CORPUS)),
        "pages": 0,
        "sparse_pages": [],
        "error": "",
        "bytes": path.stat().st_size,
    }
    try:
        document = pymupdf.open(path)
    except Exception as error:  # encrypted or corrupt file
        record["error"] = f"{type(error).__name__}: {error}"
        return record
    try:
        record["pages"] = document.page_count
        for index in range(document.page_count):
            try:
                text = document.load_page(index).get_text("text")
            except Exception as error:
                record["sparse_pages"].append({"page": index + 1, "chars": 0,
                                               "error": type(error).__name__})
                continue
            chars = len("".join((text or "").split()))
            if chars < min_chars:
                try:
                    images = len(document.load_page(index).get_images(full=True))
                except Exception:
                    images = 0
                record["sparse_pages"].append({"page": index + 1, "chars": chars,
                                               "images": images})
    finally:
        document.close()
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", nargs="?", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--json", type=Path, default=Path(__file__).with_name(
        "ocr_coverage_report.json"))
    parser.add_argument("--min-chars", type=int, default=50)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    files = sorted(args.corpus.rglob("*.pdf"))
    records = []
    total_pages = total_sparse = 0
    for position, path in enumerate(files, start=1):
        record = audit_pdf(path, args.min_chars)
        records.append(record)
        total_pages += record["pages"]
        total_sparse += len(record["sparse_pages"])
        if not args.quiet and (position % 25 == 0 or position == len(files)):
            print(f"[{position}/{len(files)}] pages={total_pages} sparse={total_sparse}",
                  flush=True)

    sparse_files = [record for record in records if record["sparse_pages"]]
    image_sparse = sum(1 for record in records for page in record["sparse_pages"]
                       if page.get("images"))
    report = {
        "corpus": str(args.corpus),
        "min_chars": args.min_chars,
        "pdf_count": len(files),
        "page_count": total_pages,
        "sparse_page_count": total_sparse,
        "sparse_image_page_count": image_sparse,
        "sparse_pdf_count": len(sparse_files),
        "errors": [record for record in records if record["error"]],
        "files": records,
    }
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "pdf_count", "page_count", "sparse_page_count", "sparse_pdf_count")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
