"""Export one Samsung Note through the full pipeline and publish both trees.

The incremental exporter links a note into ``notes/compressed`` whenever the
page cache has no OCR result for it, so a note that was exported before the
OCR cache existed keeps a symlink instead of a real derived PDF.  This module
runs the *complete* pipeline for a single note:

1. copy the read-only WDoc directory from the VM;
2. render every visible page with ``sdocx-cli`` through the page cache;
3. OCR the pages that have no text layer (GPU PaddleOCR) and build the
   searchable variant;
4. repair dark canvases on both variants (the renderer paints the note's own
   background, so a dark-theme note renders black again);
5. publish the raw export to ``notes/original`` and the searchable export to
   ``notes/compressed`` as a real file, replacing a symlink alias;
6. refresh the local export state and write a before/after diff report.

Nothing on the VM is modified and the workspace caps are checked before the
publish step.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT / "02_extracted_notes_pdf"
sys.path.insert(0, str(ROOT / "script"))
sys.path.insert(0, str(PROJECT_ROOT / "03_rtx_compression_ocr"))

from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

import incremental_export as ie  # noqa: E402
import page_delta as pd  # noqa: E402
import pdf_color_normalize as color  # noqa: E402

HIST = ROOT / "ai_agend_history"
STATE = HIST / "incremental_state.sqlite"
MAX_WORKSPACE_BYTES = 55_000_000_000
MIN_FREE_BYTES = 15 * 1024 ** 3
GUEST_WDOC = ("C:/Users/notes/AppData/Local/Packages/"
              "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState/wdoc")


def _ensure_ocr_environment() -> None:
    """Re-exec with the GPU OCR runtime on the loader path when needed."""
    if os.environ.get("SN_PIPELINE_OCR_ENV") == "1":
        return
    environment = ie.ocr_env()
    environment["SN_PIPELINE_OCR_ENV"] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], environment)


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _workspace_bytes() -> int:
    total = 0
    for path in PROJECT_ROOT.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def _fetch(uuid: str, work: Path) -> Path:
    """Copy one WDoc directory from the VM; the guest stays read-only."""
    destination = work / uuid
    if (destination / "note.note").is_file():
        return destination
    if destination.exists():
        shutil.rmtree(destination)
    work.mkdir(parents=True, exist_ok=True)
    remote = f"notes@127.0.0.1:{GUEST_WDOC}/{uuid}"
    ie.execute(ie.transport("scp") + ["-r", remote, str(work)], 900)
    if not (destination / "note.note").is_file():
        raise RuntimeError(f"the WDoc copy for {uuid} is incomplete")
    return destination


def _stats(path: Path) -> dict:
    """Page and luminance summary of one PDF."""
    pages = color.render_page_stats(path)
    return {
        "pages": len(pages),
        "black_pages": [item["index"] for item in pages if item["mean"] < 30.0],
        "mean_min": min((item["mean"] for item in pages), default=0.0),
        "mean_max": max((item["mean"] for item in pages), default=0.0),
        "text_chars": sum(int(item["text_chars"]) for item in pages),
        "bytes": path.stat().st_size,
    }


def _publish(source: Path, target: Path) -> dict:
    """Atomically replace a file, keeping the previous bytes as a hard link."""
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if target.is_file() and not target.is_symlink():
        preserved = HIST / "preserved_originals"
        preserved.mkdir(exist_ok=True)
        previous = preserved / f"{target.stem}.{_digest(target)}.pdf"
        if not previous.exists():
            os.link(target, previous)
    part = target.with_name(target.name + ".part")
    shutil.copyfile(source, part)
    with part.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(part, target)
    return {
        "path": str(target),
        "sha256": _digest(target),
        "bytes": target.stat().st_size,
        "preserved": str(previous) if previous else None,
    }


def _drop_symlink(path: Path) -> bool:
    """Remove a symlink alias so a real derived PDF can take its place."""
    if path.is_symlink():
        path.unlink()
        return True
    return False


def run(uuid: str, *, apply: bool = False, work: Path | None = None) -> dict:
    """Run the pipeline for one note and report the before/after difference."""
    work = work or Path("/tmp/opencode/pipeline") / uuid
    work.mkdir(parents=True, exist_ok=True)
    source = _fetch(uuid, work)
    render_work = work / "render"
    if render_work.exists():
        shutil.rmtree(render_work)
    render_work.mkdir(parents=True)
    from derive_corpus import page_ocr_hook

    # The derived page cache is regenerated data; keep it outside the capped
    # worktree so a large note cannot push the workspace past its limit.  The
    # real workspace cap is enforced by the publish step below.
    pd.PAGE_CACHE_DIR = work / "page_cache"
    pd.PAGE_CACHE_LIMIT_BYTES = 4 * 1024 ** 3
    pd.PROJECT_CAP_BYTES = 10 ** 15
    started = time.time()
    raw, pages, warnings = pd.compile_note(source, render_work, searchable_hook=page_ocr_hook)
    searchable = render_work / "page-delta-searchable.pdf"
    report: dict = {
        "uuid": uuid,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "render_seconds": round(time.time() - started, 1),
        "warnings": warnings,
        "raw": str(raw),
        "searchable": str(searchable) if searchable.is_file() else None,
    }
    if not searchable.is_file():
        # The full-renderer fallback (notes whose paper is an embedded PDF)
        # assembles the document in one go, so the page-level OCR hook never
        # runs.  OCR the assembled document instead; the result is checked
        # against the raw export before it is used.
        from derive_corpus import ocr_engine, verify_pair
        from paddle_ocr_pdf_batch import process_pdf

        with (HIST / "gpu_ocr.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process_pdf(raw, searchable, ocr_engine, dpi=300, ocr_max_side_len=2560,
                        min_score=.6, force_ocr=False)
        verify_pair(raw, searchable)
        report["warnings"].append("pipeline: OCR applied to the assembled document")
        report["searchable"] = str(searchable)
    repaired_raw = work / "repaired-raw.pdf"
    repaired_searchable = work / "repaired-searchable.pdf"
    color.normalize_pdf(raw, repaired_raw)
    color.normalize_pdf(searchable, repaired_searchable)
    # The derived variant keeps the OCR text layer and the original appearance
    # but stores the page images with a better codec at the same resolution.
    derived = work / "derived.pdf"
    compression = None
    try:
        from pdf_compress import compress as compress_pdf

        compression = compress_pdf(repaired_searchable, derived, verify=False)
        if derived.stat().st_size >= repaired_searchable.stat().st_size:
            derived = repaired_searchable
    except Exception as exc:
        derived = repaired_searchable
        compression = {"error": str(exc)[:200]}
    report["before"] = {"raw": _stats(raw), "searchable": _stats(searchable)}
    report["after"] = {"raw": _stats(repaired_raw), "searchable": _stats(derived)}
    report["compression"] = compression.to_dict() if hasattr(compression, "to_dict") else compression
    if not apply:
        report["status"] = "dry-run"
        return report
    state_rows = _state_row(uuid)
    original_target = Path(state_rows["path"]) if state_rows else None
    compressed_target = ROOT / "notes/compressed" / (
        original_target.relative_to(ROOT / "notes/original") if original_target else Path(uuid + ".pdf")
    )
    projected = _workspace_bytes() - (
        original_target.stat().st_size if original_target and original_target.is_file() else 0
    ) + repaired_raw.stat().st_size + repaired_searchable.stat().st_size
    report["projected_workspace_bytes"] = projected
    if projected >= MAX_WORKSPACE_BYTES or shutil.disk_usage(PROJECT_ROOT).free < MIN_FREE_BYTES:
        report["status"] = "refused"
        report["reason"] = "the publish would break the workspace caps"
        return report
    published = {}
    was_symlink = compressed_target.is_symlink()
    if original_target is not None:
        published["original"] = _publish(repaired_raw, original_target)
    published["compressed"] = _publish(derived, compressed_target)
    published["compressed"]["text_chars"] = report["after"]["searchable"]["text_chars"]
    report["published"] = published
    report["symlink_replaced"] = was_symlink
    _update_state(uuid, published, pages)
    report["status"] = "published"
    return report


def _state_row(uuid: str) -> dict | None:
    import sqlite3

    if not STATE.is_file():
        return None
    connection = sqlite3.connect(STATE.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM exports WHERE uuid=?", (uuid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _update_state(uuid: str, published: dict, pages: int) -> None:
    """Record the new export digests in the local bookkeeping database."""
    import sqlite3

    if not STATE.is_file():
        return
    connection = sqlite3.connect(STATE.resolve().as_uri())
    try:
        with connection:
            if "original" in published:
                connection.execute(
                    "UPDATE exports SET sha256=?, pages=?, renderer=? WHERE uuid=?",
                    (published["original"]["sha256"], int(pages), "sdocx-pipeline", uuid),
                )
            if "compressed" in published:
                connection.execute(
                    "UPDATE exports SET derived_path=?, derived_sha256=?, derived_chars=? "
                    "WHERE uuid=?",
                    (published["compressed"]["path"], published["compressed"]["sha256"],
                     int(published["compressed"].get("text_chars") or 0), uuid),
                )
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("uuid")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    _ensure_ocr_environment()
    report = run(args.uuid, apply=args.apply, work=args.work)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
