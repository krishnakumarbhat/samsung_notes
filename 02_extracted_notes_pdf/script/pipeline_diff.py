"""Compare two exported PDFs page by page and write a human-readable diff.

The report holds the per-page luminance and text deltas plus the file level
facts, and a set of before/after page renders so the change can be inspected
without opening a PDF viewer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium

from pdf_color_normalize import render_page_stats


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _render(path: Path, page: int, target: Path, scale: float = 72 / 72) -> None:
    document = pdfium.PdfDocument(str(path))
    try:
        bitmap = document[page].render(scale=scale)
        bitmap.to_pil().save(target)
        bitmap.close()
    finally:
        document.close()


def compare(before: Path, after: Path, *, output: Path, preview_dir: Path | None = None,
            preview_pages: tuple[int, ...] = (0, 1, 3, 8, 16, 31)) -> dict:
    """Build the before/after report for one PDF pair."""
    before_stats = render_page_stats(before)
    after_stats = render_page_stats(after)
    pages = []
    for index in range(max(len(before_stats), len(after_stats))):
        old = before_stats[index] if index < len(before_stats) else None
        new = after_stats[index] if index < len(after_stats) else None
        pages.append({
            "page": index,
            "before_mean": old["mean"] if old else None,
            "after_mean": new["mean"] if new else None,
            "before_text_chars": old["text_chars"] if old else None,
            "after_text_chars": new["text_chars"] if new else None,
            "before_black": bool(old and old["mean"] < 30.0),
            "after_black": bool(new and new["mean"] < 30.0),
        })
    payload = {
        "before": {
            "path": str(before),
            "bytes": before.stat().st_size,
            "sha256": _digest(before),
            "pages": len(before_stats),
            "black_pages": [item["index"] for item in before_stats if item["mean"] < 30.0],
            "mean_min": min((item["mean"] for item in before_stats), default=0.0),
            "text_chars": sum(int(item["text_chars"]) for item in before_stats),
        },
        "after": {
            "path": str(after),
            "bytes": after.stat().st_size,
            "sha256": _digest(after),
            "pages": len(after_stats),
            "black_pages": [item["index"] for item in after_stats if item["mean"] < 30.0],
            "mean_min": min((item["mean"] for item in after_stats), default=0.0),
            "text_chars": sum(int(item["text_chars"]) for item in after_stats),
        },
        "pages": pages,
    }
    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)
        previews = []
        for page in preview_pages:
            if page >= len(before_stats) or page >= len(after_stats):
                continue
            old_png = preview_dir / f"page-{page:03d}-before.png"
            new_png = preview_dir / f"page-{page:03d}-after.png"
            _render(before, page, old_png)
            _render(after, page, new_png)
            previews.append({"page": page, "before": str(old_png), "after": str(new_png)})
        payload["previews"] = previews
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args(argv)
    payload = compare(args.before, args.after, output=args.output,
                      preview_dir=args.preview_dir)
    print(json.dumps({key: payload[key] for key in ("before", "after")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
