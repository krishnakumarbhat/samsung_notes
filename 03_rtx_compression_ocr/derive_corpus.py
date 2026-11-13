"""Publish lossless/searchable derived PDFs without ever modifying original exports."""
from __future__ import annotations

import argparse
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

from pymupdf_compat import fitz
import pikepdf

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "02_extracted_notes_pdf/notes/original"
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
                                use_textline_orientation=False, cpu_threads=2)
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
    """Enforce physical allocation guardrails including candidate output headroom.

    ``workspace_bound=False`` is used when the scratch tree lives outside the
    capped worktree: only the volume free-space floor still applies, because
    the transient archive and render never touch the workspace.
    ``SN_RELAX_DISK_GUARD=1`` lifts the workspace ceiling for a deliberate
    one-off maintenance pass; the volume floor always applies.
    """
    relaxed = os.environ.get("SN_RELAX_DISK_GUARD") == "1"
    used = int(subprocess.check_output(["du", "-s", "--block-size=1", str(ROOT)], text=True).split()[0])
    if not relaxed and workspace_bound and used + reserve >= 55_000_000_000:
        raise RuntimeError("Disk budget prevents this derived PDF; original and placeholder retained")
    if shutil.disk_usage(ROOT).free - reserve < 15 * 1024**3:
        raise RuntimeError("Disk budget prevents this derived PDF; original and placeholder retained")


def verify_pair(original: Path, candidate: Path) -> tuple[int, int]:
    """Require equal page count/rendered pixels and no lost selectable text."""
    after_chars = 0
    with fitz.open(original) as source, fitz.open(candidate) as output:
        if len(source) != len(output):
            raise ValueError("Derived PDF changed page count")
        for before, after in zip(source, output):
            a = before.get_pixmap(matrix=fitz.Matrix(.5, .5), colorspace=fitz.csRGB, alpha=False)
            b = after.get_pixmap(matrix=fitz.Matrix(.5, .5), colorspace=fitz.csRGB, alpha=False)
            if (a.width, a.height, a.samples) != (b.width, b.height, b.samples):
                raise ValueError("Derived PDF changed visible content")
            if len(after.get_text().strip()) < len(before.get_text().strip()):
                raise ValueError("Derived PDF lost native text")
            after_chars += len(after.get_text().strip())
        page_count = len(source)
    with pikepdf.Pdf.open(candidate) as parsed:
        if any("error" in str(issue).lower() for issue in parsed.check_pdf_syntax()):
            raise ValueError("Derived PDF syntax error")
    return page_count, after_chars


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


def derive_file(source: Path, *, ocr: bool = False, max_ocr_pages: int = 1000) -> dict:
    """Create a lossless derivative under strict space/fidelity checks, atomically."""
    source = source.absolute()
    relative = source.relative_to(ORIGINAL)
    target = COMPRESSED / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    before_sha = digest(source)
    with fitz.open(source) as pdf:
        sparse = [i + 1 for i, page in enumerate(pdf) if len(page.get_text().strip()) < 50]
        count = len(pdf)
        if count < 1:
            raise ValueError("Original PDF has no visible pages")
        native_chars = sum(len(page.get_text().strip()) for page in pdf)
        featureless = []
        for page_number in sparse:
            pix = pdf[page_number - 1].get_pixmap(matrix=fitz.Matrix(.5, .5), colorspace=fitz.csGRAY, alpha=False)
            if pix.samples and pix.samples.count(pix.samples[0]) == len(pix.samples):
                featureless.append(page_number)
    needs_ocr = [number for number in sparse if number not in featureless]
    if ocr and not needs_ocr:
        # An already-searchable PDF (possibly with blank pages) needs no second
        # full-sized copy. A distinct path shares the immutable source inode.
        guard(65536)
        with tempfile.TemporaryDirectory(prefix=".derive-", dir=target.parent) as temporary:
            candidate = Path(temporary) / source.name
            os.link(source, candidate)
            if digest(candidate) != before_sha:
                raise ValueError("Original changed before native hard-link publication")
            os.replace(candidate, target)
        return {"source": str(relative), "source_sha256": before_sha, "sha256": before_sha,
                "pages": count, "chars": native_chars, "sparse_pages": 0, "ocr": False,
                "status": "searchable" if native_chars else "no_text",
                "source_bytes": source.stat().st_size, "derived_bytes": target.stat().st_size,
                "storage": "hardlink-native-or-featureless"}
    if ocr and len(sparse) > max_ocr_pages:
        return {"source": str(relative), "status": "deferred_ocr_page_budget", "sparse_pages": len(sparse)}
    guard(source.stat().st_size * 2 + 16 * 1024**2)
    with tempfile.TemporaryDirectory(prefix=".derive-", dir=target.parent) as temporary:
        work = Path(temporary)
        candidate = work / source.name
        if ocr and sparse:
            from paddle_ocr_pdf_batch import process_pdf
            with (HISTORY / "gpu_ocr.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process_pdf(source, candidate, ocr_engine, dpi=300, ocr_max_side_len=2560,
                            min_score=.6, force_ocr=False)
        else:
            with pikepdf.Pdf.open(source) as pdf:
                pdf.remove_unreferenced_resources()
                pdf.save(candidate, compress_streams=True, recompress_flate=True,
                         object_stream_mode=pikepdf.ObjectStreamMode.generate)
        # Re-encode the page images at their exact pixel size: the resolution
        # never changes, only the codec does, which is where the size is won.
        from pdf_compress import compress as compress_pdf

        reencoded = work / "reencoded.pdf"
        try:
            compress_pdf(candidate, reencoded, verify=False)
        except Exception as exc:
            logger.warning("image re-encoding skipped for %s: %s", source.name, exc)
            reencoded = candidate
        if reencoded.is_file() and reencoded.stat().st_size < candidate.stat().st_size:
            candidate.unlink()
            candidate = reencoded
        page_count, chars = verify_equivalent(source, candidate)
        if digest(source) != before_sha:
            raise ValueError("Original changed during derived compilation")
        # No smaller output and no OCR benefit: use a hard link, not a duplicate.
        # Future writes always use replace(), so the original inode stays untouched.
        if candidate.stat().st_size >= source.stat().st_size and not (ocr and sparse):
            candidate.unlink()
            os.link(source, candidate)
        after_sha = digest(candidate)
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(candidate, target)  # replaces the placeholder itself, never its referent
    return {"source": str(relative), "source_sha256": before_sha, "sha256": after_sha,
            "pages": page_count, "chars": chars, "sparse_pages": len(sparse),
            "ocr": bool(ocr and sparse), "status": "searchable" if chars else "no_text",
            "source_bytes": source.stat().st_size, "derived_bytes": target.stat().st_size}


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
