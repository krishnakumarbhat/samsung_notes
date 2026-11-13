"""Recompress the derived PDF tree without changing any resolution.

The derived files already carry their OCR text layer, so they are re-encoded
in place: the page images are written with a better codec at exactly the same
pixel size, duplicated images are shared, and the document is rewritten with
object streams.  Every candidate is verified against its original - page
count, page geometry, image dimensions, selectable text, and the rendered
appearance - before it replaces the file.  A file that cannot be verified is
left untouched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
import time
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

from derive_corpus import verify_equivalent  # noqa: E402
from pdf_compress import compress as compress_pdf  # noqa: E402

REPORT = PROJECT_ROOT / "00_orchestrator/RECOMPRESSION_REPORT.json"
ROLLBACK = Path("/tmp/opencode/recompress_rollback")


def _matches_original(original: Path, derived: Path) -> bool:
    """Whether the derived file still has the original's page structure."""
    import pikepdf

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


def recompress_one(original: Path, derived: Path, *, quality: int = 88,
                   apply: bool = True) -> dict:
    """Recompress one derived file and publish it when it is smaller and sound."""
    result = {
        "original": str(original),
        "derived": str(derived),
        "before_bytes": derived.stat().st_size,
        "status": "skipped",
    }
    if not derived.is_file() or derived.is_symlink():
        result["status"] = "not-a-regular-file"
        return result
    if not original.is_file():
        result["status"] = "no-original"
        return result
    if not _matches_original(original, derived):
        result["status"] = "structure-mismatch"
        return result
    with tempfile.TemporaryDirectory(prefix=".recompress-", dir=derived.parent) as temporary:
        candidate = Path(temporary) / derived.name
        report = compress_pdf(derived, candidate, jpeg_quality=quality, verify=False)
        result["images_reencoded"] = report.images_reencoded
        result["images_deduplicated"] = report.images_deduplicated
        result["candidate_bytes"] = candidate.stat().st_size
        if candidate.stat().st_size >= derived.stat().st_size:
            result["status"] = "not-smaller"
            result["ratio"] = 1.0
            return result
        try:
            pages, chars = verify_equivalent(original, candidate)
        except Exception as exc:
            result["status"] = "verification-failed"
            result["reason"] = str(exc)[:200]
            return result
        result["verified_pages"] = pages
        result["text_chars"] = chars
        if apply:
            ROLLBACK.mkdir(parents=True, exist_ok=True)
            backup = ROLLBACK / derived.relative_to(NOTES)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(derived, backup)
            shutil.copyfile(candidate, derived)
        result["status"] = "recompressed" if apply else "dry-run"
        result["after_bytes"] = candidate.stat().st_size
        result["ratio"] = round(result["before_bytes"] / max(1, result["after_bytes"]), 3)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality", type=int, default=88)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    results = []
    before_total = 0
    after_total = 0
    started = time.time()
    candidates = [path for path in sorted(COMPRESSED.rglob("*.pdf"))
                  if path.is_file() and not path.is_symlink()]
    if args.limit:
        candidates = candidates[:args.limit]
    for index, derived in enumerate(candidates, 1):
        original = ORIGINAL / derived.relative_to(COMPRESSED)
        result = recompress_one(original, derived, quality=args.quality, apply=args.apply)
        results.append(result)
        before_total += result["before_bytes"]
        after_total += result.get("after_bytes", result["before_bytes"])
        if index % 10 == 0 or result["status"] not in {"recompressed", "not-smaller"}:
            print(f"[{index}/{len(candidates)}] {result['status']} "
                  f"{derived.name[:60]} {result.get('ratio', 1.0)}x", flush=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "applied": args.apply,
        "quality": args.quality,
        "files": len(results),
        "seconds": round(time.time() - started, 1),
        "bytes_before": before_total,
        "bytes_after": after_total,
        "saved_bytes": before_total - after_total,
        "ratio": round(before_total / max(1, after_total), 3) if after_total else 1.0,
        "status_counts": {
            status: sum(1 for item in results if item["status"] == status)
            for status in sorted({item["status"] for item in results})
        },
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2))
    print(json.dumps({key: payload[key] for key in
                      ("files", "bytes_before", "bytes_after", "saved_bytes",
                       "ratio", "status_counts", "seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
