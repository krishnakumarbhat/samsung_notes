#!/usr/bin/env python3
"""Build a compact metadata manifest for the exported PDF corpus.

The PDFs themselves are deliberately not committed.  This manifest records the
logical source, physical path, page count, extractable text volume and SHA-256
so a restored machine can prove it has the same corpus without publishing the
notes.

Usage
    source 00_orchestrator/runtime.sh
    python scripts/corpus_manifest.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "02_extracted_notes_pdf/notes/original"
COMPRESSED = ROOT / "02_extracted_notes_pdf/notes/compressed"
OUTPUT = ROOT / "manifests/corpus.json"
UUID = re.compile(r"__?(c8860b15-[0-9a-f-]{27,})")


def _fitz() -> Any:
    """Import PyMuPDF under either module name."""
    try:
        import fitz  # type: ignore

        return fitz
    except Exception:
        import pymupdf  # type: ignore

        return pymupdf


def _sha256(path: Path) -> str:
    """Hash a PDF with bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect(path: Path, fitz: Any) -> dict:
    """Return page, text and status metadata for one PDF."""
    text_chars = 0
    pages = 0
    searchable_pages = 0
    error = ""
    try:
        with fitz.open(path) as document:
            pages = len(document)
            for page in document:
                try:
                    text = page.get_text("text")
                except Exception as exc:  # pragma: no cover - malformed PDFs
                    error = f"{type(exc).__name__}: {exc}"
                    continue
                clean = "".join((text or "").split())
                text_chars += len(clean)
                if clean:
                    searchable_pages += 1
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    status = "unreadable" if error and pages == 0 else ("searchable" if text_chars else "empty")
    return {
        "pages": pages,
        "text_chars": text_chars,
        "searchable_pages": searchable_pages,
        "status": status,
        "error": error,
    }


def main(argv: list[str] | None = None) -> int:
    """Write the corpus manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--no-hash", action="store_true", help="skip SHA-256 (faster)")
    parser.add_argument(
        "--include-paths",
        action="store_true",
        help="include per-PDF names and hashes (private; do not publish)",
    )
    args = parser.parse_args(argv)

    if not ORIGINAL.is_dir():
        raise SystemExit(f"missing corpus: {ORIGINAL}")
    fitz = _fitz()
    all_files: list[dict] = []
    files: list[dict] = []
    for path in sorted(ORIGINAL.rglob("*.pdf")):
        relative = path.relative_to(ROOT).as_posix()
        compressed = COMPRESSED / path.relative_to(ORIGINAL)
        record: dict = {
            "physical_path": relative,
            "compressed_path": compressed.relative_to(ROOT).as_posix() if compressed.exists() else None,
            "bytes": path.stat().st_size,
        }
        match = UUID.search(path.stem)
        record["uuid"] = match.group(1) if match else None
        record.update(_inspect(path, fitz))
        if not args.no_hash:
            record["sha256"] = _sha256(path)
        all_files.append(record)
        if args.include_paths:
            files.append(record)

    payload = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(ORIGINAL.relative_to(ROOT)),
        "totals": {
            "files": len(all_files),
            "pages": sum(item["pages"] for item in all_files),
            "text_chars": sum(item["text_chars"] for item in all_files),
            "searchable": sum(item["status"] == "searchable" for item in all_files),
            "empty": sum(item["status"] == "empty" for item in all_files),
            "unreadable": sum(item["status"] == "unreadable" for item in all_files),
            "bytes": sum(item["bytes"] for item in all_files),
        },
        "files": files,
        "privacy": "Per-file names omitted; use --include-paths for a private local manifest.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {args.output}: {payload['totals']['files']} PDFs, "
        f"{payload['totals']['pages']} pages, {payload['totals']['searchable']} searchable, "
        f"{payload['totals']['empty']} empty"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
