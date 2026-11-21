"""Publish lossless/searchable derived PDFs without ever modifying original exports."""
from __future__ import annotations

import argparse
import atexit
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pymupdf_compat import fitz
import pikepdf

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
SAM_NOTES_ROOT = Path(os.environ.get("SAM_NOTES_ROOT", str(PROJECT_ROOT.parent / "sam_notes"))).expanduser()
_OWN_RUN_ROOT = not os.environ.get("SN_EXPORT_WORK")
_DEFAULT_RUN_ROOT = SAM_NOTES_ROOT / f"_run_tmp_{os.getpid()}"
WORK_ROOT = Path(
    os.environ.get("SN_EXPORT_WORK", str(_DEFAULT_RUN_ROOT))
).expanduser()
if _OWN_RUN_ROOT:
    atexit.register(shutil.rmtree, WORK_ROOT, ignore_errors=True)
ORIGINAL = WORK_ROOT / "original"
COMPRESSED = ROOT / "02_extracted_notes_pdf/notes/compressed"
HISTORY = ROOT / "02_extracted_notes_pdf/ai_agend_history"
MANIFEST = HISTORY / "derived_manifest.json"
_OCR_ENGINE = None


def ocr_engine():
    """Reuse one light detector/recognizer across a sequential GPU batch."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from paddle_ocr_pdf_batch import build_ocr
        _OCR_ENGINE = build_ocr(lang="en", device_mode="gpu", ocr_max_side_len=2560,
                                use_textline_orientation=False, cpu_threads=8)
    return _OCR_ENGINE


ocr_engine.cache_profile = "en:mobile-det:server-rec:orientation=False"


def digest(path: Path) -> str:
    """Hash with bounded-memory streaming reads."""
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def page_ocr_hook(raw: Path, page_id: str, work: Path) -> Path:
    """Lazy shared RTX engine; OCR/compress only a new/changed cached page."""
    from paddle_ocr_pdf_batch import process_pdf

    output = work / f"{page_id}.searchable.pdf"
    with (HISTORY / "gpu_ocr.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process_pdf(raw, output, ocr_engine, dpi=300, ocr_max_side_len=2560, min_score=.6, force_ocr=False)
    verify_pair(raw, output)
    return output


page_ocr_hook.cache_key = "paddle-ocr-v5-selective300-2560-sandwich3-" + hashlib.sha256(
    (ROOT / "03_rtx_compression_ocr/paddle_ocr_pdf_batch.py").read_bytes()
).hexdigest()[:16]


def guard(reserve: int = 0, *, workspace_bound: bool = True) -> None:
    """Enforce the 55 GB ceiling and free-space floor, including scratch.

    The legacy workspace_bound argument is accepted but cannot bypass the cap.
    """
    used = int(subprocess.check_output(["du", "-s", "--block-size=1", str(ROOT)], text=True).split()[0])
    if used + reserve >= 55_000_000_000:
        raise RuntimeError("Disk budget prevents this derived PDF; original and placeholder retained")
    if shutil.disk_usage(ROOT).free - reserve < 15 * 1024**3:
        raise RuntimeError("Disk budget prevents this derived PDF; original and placeholder retained")


def verify_pair(original: Path, candidate: Path) -> tuple[int, int]:
    """Require exact rendering, searchable text and navigational semantics."""
    after_chars = 0
    with fitz.open(original) as source, fitz.open(candidate) as output:
        if len(source) != len(output):
            raise ValueError("Derived PDF changed page count")
        if _semantic_value(source.get_toc(simple=False)) != _semantic_value(output.get_toc(simple=False)):
            raise ValueError("Derived PDF changed outline destinations")
        if hasattr(source, "resolve_names") and _semantic_value(source.resolve_names()) != _semantic_value(output.resolve_names()):
            raise ValueError("Derived PDF changed named destinations")
        if source.get_page_labels() != output.get_page_labels():
            raise ValueError("Derived PDF changed page labels")
        for before, after in zip(source, output):
            if (tuple(before.mediabox), tuple(before.cropbox), before.rotation) != (tuple(after.mediabox), tuple(after.cropbox), after.rotation):
                raise ValueError("Derived PDF changed page geometry")
            if _semantic_value(before.get_links()) != _semantic_value(after.get_links()):
                raise ValueError("Derived PDF changed hyperlinks or index jumps")
            before_annots = [(a.type, a.info, tuple(a.rect)) for a in before.annots() or ()]
            after_annots = [(a.type, a.info, tuple(a.rect)) for a in after.annots() or ()]
            if _semantic_value(before_annots) != _semantic_value(after_annots):
                raise ValueError("Derived PDF changed annotations")
            a = before.get_pixmap(matrix=fitz.Matrix(.5, .5), colorspace=fitz.csRGB, alpha=False)
            b = after.get_pixmap(matrix=fitz.Matrix(.5, .5), colorspace=fitz.csRGB, alpha=False)
            if not _visible_render_matches(a, b):
                raise ValueError("Derived PDF changed visible content")
            old_text = " ".join(before.get_text().split())
            new_text = " ".join(after.get_text().split())
            if old_text and old_text not in new_text:
                raise ValueError("Derived PDF lost native text")
            after_chars += len(after.get_text().strip())
        page_count = len(source)
    with pikepdf.Pdf.open(candidate) as parsed:
        if any("error" in str(issue).lower() for issue in parsed.check_pdf_syntax()):
            raise ValueError("Derived PDF syntax error")
    return page_count, after_chars


def _visible_render_matches(before: Any, after: Any) -> bool:
    """Accept only sub-pixel rasterizer noise, never meaningful page changes.

    OCR adds invisible text and PDF reserialization may move antialias values by
    one or two RGB levels across library builds.  Byte equality of raster
    samples made otherwise equivalent searchable derivatives fail closed.  The
    tolerance is intentionally tiny: dimensions must match, mean channel drift
    must stay below 0.05, and no more than 0.01% of pixels may differ by >2.
    """
    if before.width != after.width or before.height != after.height:
        return False
    if before.samples == after.samples:
        return True
    import numpy as np

    left = np.frombuffer(before.samples, dtype=np.uint8).astype(np.int16)
    right = np.frombuffer(after.samples, dtype=np.uint8).astype(np.int16)
    if left.shape != right.shape or left.size == 0 or left.size % 3:
        return False
    difference = np.abs(left - right).reshape((-1, 3))
    mean_drift = float(difference.mean())
    changed = np.any(difference > 2, axis=1)
    return mean_drift <= 0.05 and float(changed.mean()) <= 0.0001


def normalize_light_canvas(source: Path, work: Path) -> tuple[Path, list[str]]:
    """Whiten provably dark note canvases before OCR and final validation.

    The source is never modified.  Embedded-PDF paper must be handled by the
    template recovery layer, because changing the source document would destroy
    the user's original paper colors.  The caller is responsible for skipping
    those notes.  Dark pages are detected from raster statistics, then the
    structural normalizer changes only a page-sized dark fill or a dark image on
    a dark page.  A still-dark page fails closed.
    """
    source, work = Path(source), Path(work)
    import importlib

    try:
        color = importlib.import_module("pdf_color_normalize")
    except ModuleNotFoundError:
        color = importlib.import_module(
            "02_extracted_notes_pdf.script.pdf_color_normalize"
        )
    normalize_pdf = color.normalize_pdf
    render_page_stats = color.render_page_stats

    stats = render_page_stats(source, scale=12 / 72)
    dark_pages = [
        int(item["index"])
        for item in stats
        if float(item.get("mean", 255)) < 80
        and float(item.get("white_fraction", 0)) < 0.35
    ]
    if not dark_pages:
        return source, []

    output = work / f"{source.stem}.light-canvas.pdf"
    report = normalize_pdf(source, output, dark_pages=dark_pages)
    repaired = set(getattr(report, "repaired_pages", []))
    if not repaired:
        raise ValueError("dark canvas detected but no safe light-canvas repair was proven")
    remaining = [
        int(item["index"])
        for item in render_page_stats(output, scale=12 / 72)
        if int(item["index"]) in repaired
        and float(item.get("mean", 255)) < 80
        and float(item.get("white_fraction", 0)) < 0.35
    ]
    if remaining:
        raise ValueError(f"dark canvas remains after normalization on page(s): {remaining[:8]}")
    with fitz.open(source) as before, fitz.open(output) as after:
        if len(before) != len(after):
            raise ValueError("light-canvas normalization changed page count")
        for left, right in zip(before, after):
            if (tuple(left.mediabox), tuple(left.cropbox), left.rotation) != (
                tuple(right.mediabox), tuple(right.cropbox), right.rotation
            ):
                raise ValueError("light-canvas normalization changed page geometry")
            old_text = " ".join(left.get_text().split())
            new_text = " ".join(right.get_text().split())
            if old_text and old_text not in new_text:
                raise ValueError("light-canvas normalization lost native text")
    return output, [f"light-canvas-normalized:{','.join(map(str, sorted(repaired)))}"]


def _semantic_value(value: Any) -> Any:
    """Normalize navigation values while excluding serialization-only IDs."""
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()
                if key not in ("xref", "id")}
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    if isinstance(value, (fitz.Point, fitz.Rect)):
        return [round(float(item), 5) for item in value]
    if isinstance(value, float):
        return round(value, 5)
    return value


def verify_equivalent(original: Path, candidate: Path, *, max_mean_drift: float = 3.0,
                      max_std_drift: float = 8.0, scale: float = 12 / 72) -> tuple[int, int]:
    """Require a resolution-preserving derivative of the source PDF.

    The derived file may be re-encoded (a lossless page image becomes a JPEG
    of the same pixel size), so the pixels are not compared byte for byte.
    Instead the page count, the page geometry, the image dimensions, the
    selectable text, and the rendered appearance are checked.
    """
    import numpy as np
    import pypdfium2 as pdfium

    def render(path: Path) -> list[tuple[float, float]]:
        document = pdfium.PdfDocument(str(path))
        stats: list[tuple[float, float]] = []
        try:
            for index in range(len(document)):
                page = document[index]
                try:
                    bitmap = page.render(scale=scale, grayscale=True)
                    array = bitmap.to_numpy()
                    stats.append((float(array.mean()), float(array.std())))
                    bitmap.close()
                finally:
                    page.close()
        finally:
            document.close()
        return stats

    def images(path: Path) -> list[set[tuple[int, int]]]:
        from pdf_compress import _page_images
        with pikepdf.Pdf.open(path) as pdf:
            sizes: list[set[tuple[int, int]]] = []
            for page in pdf.pages:
                found: set[tuple[int, int]] = set()
                try:
                    resources = page.Resources.get("/XObject", {})
                except Exception:
                    resources = {}
                for _name, obj in resources.items():
                    try:
                        if str(obj.get("/Subtype")) == "/Image":
                            found.add((int(obj.get("/Width")), int(obj.get("/Height"))))
                    except Exception:
                        continue
                sizes.append(found)
            return sizes

    with fitz.open(original) as source, fitz.open(candidate) as output:
        if len(source) != len(output):
            raise ValueError("Derived PDF changed page count")
        before_chars = 0
        after_chars = 0
        for before, after in zip(source, output):
            if [float(v) for v in before.mediabox] != [float(v) for v in after.mediabox]:
                raise ValueError("Derived PDF changed page geometry")
            if len(after.get_text().strip()) < len(before.get_text().strip()):
                raise ValueError("Derived PDF lost native text")
            before_chars += len(before.get_text().strip())
            after_chars += len(after.get_text().strip())
    if images(original) != images(candidate):
        raise ValueError("Derived PDF changed an image resolution")
    before_stats = render(original)
    after_stats = render(candidate)
    for index, (old, new) in enumerate(zip(before_stats, after_stats), 1):
        if abs(old[0] - new[0]) > max_mean_drift:
            raise ValueError(
                f"Derived PDF page {index} changed appearance "
                f"(mean {old[0]:.2f} -> {new[0]:.2f})"
            )
        if abs(old[1] - new[1]) > max_std_drift:
            raise ValueError(f"Derived PDF page {index} changed contrast")
    with pikepdf.Pdf.open(candidate) as parsed:
        if any("error" in str(issue).lower() for issue in parsed.check_pdf_syntax()):
            raise ValueError("Derived PDF syntax error")
    return len(before_stats), after_chars


def share_source(source: Path, candidate: Path) -> None:
    """Stage independent bytes, never a link whose source may be deleted."""
    candidate.unlink(missing_ok=True)
    shutil.copyfile(source, candidate)


def _preserve_previous_target(target: Path, new_sha: str) -> Path | None:
    """Move/copy a differing previous PDF into private history before replacement.

    A regular target is moved atomically. A valid symlink is preserved as the
    bytes it resolves to, while a broken symlink is archived as a symlink.
    Equal-content replacements need no backup. The returned backup path lets
    the caller restore a moved old target if atomic publication subsequently
    fails.
    """
    if not (target.exists() or target.is_symlink()):
        return None
    try:
        old_sha = digest(target)
    except OSError:
        old_sha = None
    if old_sha == new_sha:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns()}"
    relative = target.relative_to(COMPRESSED)
    backup = HISTORY / "preserved_originals" / "replaced_exports" / stamp / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists() or backup.is_symlink():
        backup = backup.with_name(f"{backup.stem}-{time.time_ns()}{backup.suffix}")
    if target.is_symlink() and old_sha is not None:
        # A relative symlink can break when relocated. Preserve its PDF bytes
        # independently; the referent itself is never unlinked.
        shutil.copy2(target.resolve(), backup)
        return None
    os.replace(target, backup)
    return backup


def derive_file(source: Path, *, target: Path | None = None, ocr: bool = False,
                max_ocr_pages: int = 1000) -> dict:
    """Atomically publish the smallest verified lossless standalone candidate.

    Existing compression algorithms are used in strict lossless mode; the PDF
    graph is never rasterized. If optimization cannot preserve every checked
    feature, exact source bytes are used rather than silently losing content.
    """
    source = source.absolute()
    if source.is_symlink() or not source.is_file():
        raise ValueError("Compression requires a real staged PDF")
    try:
        relative = source.relative_to(ORIGINAL)
    except ValueError:
        relative = Path(source.name)
        if target is None:
            raise ValueError("Non-staging source needs an explicit target")
    target = Path(target) if target is not None else COMPRESSED / relative
    if target.absolute() == source:
        raise ValueError("Source and destination must differ")
    target.absolute().relative_to(COMPRESSED.absolute())
    target.parent.resolve().relative_to(COMPRESSED.resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    before_sha = digest(source)
    with fitz.open(source) as pdf:
        sparse = [i + 1 for i, page in enumerate(pdf) if not page.get_text().strip()]
        count = len(pdf)
        if count < 1:
            raise ValueError("Original PDF has no visible pages")
        native_chars = sum(len(page.get_text().strip()) for page in pdf)
    if ocr and len(sparse) > max_ocr_pages:
        raise ValueError("OCR page budget exceeded; previous output retained")
    guard(source.stat().st_size * 3 + 16 * 1024**2)
    warnings = []
    did_ocr = False
    with pikepdf.open(source) as parsed:
        immutable = (parsed.is_encrypted or "/Perms" in parsed.Root or any(
            isinstance(obj, pikepdf.Dictionary) and str(obj.get("/Type")) == "/Sig"
            for obj in parsed.objects))
    if immutable and ocr:
        raise ValueError("Signed/encrypted PDFs cannot be changed safely for OCR")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if os.stat(WORK_ROOT).st_dev != os.stat(target.parent).st_dev:
        raise RuntimeError(
            "SN_EXPORT_WORK and the PDF destination must share a filesystem "
            "for atomic publication; use the sibling sam_notes run scratch"
        )
    with tempfile.TemporaryDirectory(prefix=".derive-", dir=WORK_ROOT,
                                     ignore_cleanup_errors=True) as temporary:
        work = Path(temporary)
        base = source
        if ocr and sparse:
            from paddle_ocr_pdf_batch import process_pdf
            base = work / "searchable.pdf"
            with (HISTORY / "gpu_ocr.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process_pdf(source, base, ocr_engine, dpi=300, ocr_max_side_len=2560,
                            min_score=.6, force_ocr=False)
            verify_pair(source, base)
            did_ocr = True
        from pdf_compress import compress as compress_pdf
        candidate = work / "optimized.pdf"
        try:
            if immutable:
                share_source(base, candidate)
            else:
                compress_pdf(base, candidate, lossless=True, verify=True)
            page_count, chars = verify_pair(base, candidate)
        except Exception as exc:
            warnings.append(f"Lossless optimization fallback: {type(exc).__name__}: {exc}")
            share_source(base, candidate)
            page_count, chars = verify_pair(base, candidate)
        if candidate.stat().st_size >= base.stat().st_size:
            share_source(base, candidate)
        if not did_ocr:
            # The exact-byte fallback is already structurally identical.
            chars = native_chars
        if digest(source) != before_sha:
            raise ValueError("Original changed during derived compilation")
        after_sha = digest(candidate)
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        guard()
        previous = _preserve_previous_target(target, after_sha)
        try:
            os.replace(candidate, target)
        except Exception:
            if previous is not None and not (target.exists() or target.is_symlink()):
                os.replace(previous, target)
            raise
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {"source": str(relative), "source_sha256": before_sha, "sha256": after_sha,
            "pages": page_count, "chars": chars, "sparse_pages": len(sparse),
            "ocr": did_ocr, "status": "searchable" if chars else "no_text",
            "source_bytes": source.stat().st_size, "derived_bytes": target.stat().st_size,
            "storage": "standalone", "warnings": warnings}


def process_corpus(ocr: bool = False, limit: int | None = None,
                   max_ocr_pages: int = 1000) -> dict:
    """Resume by source hash; leave explicitly deferred/unreadable PDFs untouched."""
    HISTORY.mkdir(exist_ok=True)
    with (HISTORY / "derived.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entries = json.loads(MANIFEST.read_text()).get("files", {}) if MANIFEST.exists() else {}
        processed = 0
        paths = sorted(ORIGINAL.rglob("*.pdf"), key=lambda p: p.stat().st_size)
        for path in paths:
            relative = path.relative_to(ORIGINAL).as_posix()
            old = entries.get(relative, {})
            target = COMPRESSED / relative
            sha = digest(path)
            if (old.get("source_sha256") == sha and target.is_file() and not target.is_symlink()
                    and (not ocr or old.get("ocr") or old.get("sparse_pages") == 0)):
                continue
            if limit is not None and processed >= limit:
                break
            try:
                row = derive_file(path, ocr=ocr, max_ocr_pages=max_ocr_pages)
            except Exception as error:
                row = {"source": relative, "status": "failed", "error": str(error)[:250]}
            entries[relative] = row
            processed += 1
            temp = MANIFEST.with_suffix(".tmp")
            temp.write_text(json.dumps({"files": entries, "updated": time.time()}, indent=2))
            os.replace(temp, MANIFEST)
            print(json.dumps({"processed": processed, "status": row["status"], "pages": row.get("pages")}), flush=True)
            if "Disk budget" in row.get("error", ""):
                break
    return {"processed": processed, "files": len(entries),
            "failed": sum(row["status"] == "failed" for row in entries.values()),
            "searchable": sum(row["status"] == "searchable" for row in entries.values())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-ocr-pages", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(process_corpus(args.ocr, args.limit, args.max_ocr_pages)))
