#!/usr/bin/env python3
"""Create searchable PDFs with a native-text-first PaddleOCR workflow.

Usage:
    python3 paddle_ocr_pdf_batch.py
    python3 paddle_ocr_pdf_batch.py /path/to/file-or-directory
    python3 paddle_ocr_pdf_batch.py /path/to/input --output-dir /path/to/out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ctypes
import importlib
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable


def _infer_runtime_variant_from_argv(argv: list[str]) -> str:
    for arg in argv[1:]:
        if arg.startswith("--device="):
            value = arg.split("=", 1)[1].strip().lower()
            return "cpu" if value == "cpu" else "gpu"

    for index, arg in enumerate(argv[1:], start=1):
        if arg == "--device" and index + 1 < len(argv):
            value = argv[index + 1].strip().lower()
            return "cpu" if value == "cpu" else "gpu"

    return "gpu"


os.environ.setdefault("SAM_PDF_PADDLE_VARIANT", _infer_runtime_variant_from_argv(sys.argv))

from python_runtime_bootstrap import bootstrap_python_paths

BOOTSTRAPPED_PATHS = bootstrap_python_paths(Path(__file__).resolve())

os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from paddleocr import PaddleOCR

from pymupdf_compat import fitz as imported_fitz

fitz: Any = imported_fitz
PDFPage = Any
PDFRect = Any

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("paddle_ocr_pdf_batch")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process a PDF file or directory and create searchable copies.",
    )
    parser.add_argument("input_path", type=Path, help="PDF file or directory to process")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Output directory. If omitted, a sibling searchable folder is created "
            "automatically."
        ),
    )
    parser.add_argument("--lang", default="en", help="PaddleOCR language code (default: en)")
    parser.add_argument("--dpi", type=int, default=360, help="Render DPI for OCR (default: 360)")
    parser.add_argument(
        "--ocr-max-side-len",
        type=int,
        default=4000,
        help="Maximum OCR image side length before downscaling (default: 4000)",
    )
    parser.add_argument(
        "--use-textline-orientation",
        action="store_true",
        help="Enable text line orientation correction. Slower, but helps rotated text.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.60,
        help="Minimum OCR confidence to embed text (default: 0.60)",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=None,
        help="CPU thread count for Paddle inference (default: auto, capped at 8)",
    )
    parser.add_argument("--device", choices=["auto", "cpu", "gpu"], default="auto")
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Run OCR even when the page already contains searchable PDF text.",
    )
    return parser.parse_args()


def resolve_input_path(input_path: Path) -> Path:
    resolved = input_path.expanduser().resolve()

    if not resolved.exists():
        raise SystemExit(f"Input path not found: {resolved}")

    if resolved.is_file() and resolved.suffix.lower() != ".pdf":
        raise SystemExit(f"Input file is not a PDF: {resolved}")

    if not resolved.is_file() and not resolved.is_dir():
        raise SystemExit(f"Input path is neither a PDF nor a directory: {resolved}")

    return resolved


def detect_gpu_available() -> bool:
    try:
        paddle = importlib.import_module("paddle")

        if not paddle.device.is_compiled_with_cuda():
            logger.info("Installed Paddle runtime is CPU-only. GPU OCR is unavailable in this environment.")
            return False

        if paddle.device.cuda.device_count() <= 0:
            logger.warning("Paddle was built with CUDA support, but no CUDA devices are visible.")
            return False

        cudnn_candidates = [
            "libcudnn.so",
            "libcudnn.so.9",
            "libcudnn.so.8",
            "libcudnn_cnn_infer.so.8",
            "libcudnn_ops_infer.so.8",
        ]
        for lib_name in cudnn_candidates:
            try:
                ctypes.CDLL(lib_name)
                return True
            except OSError:
                continue

        logger.warning("CUDA detected but cuDNN runtime library is not loadable. Using CPU.")
        return False
    except Exception as exc:
        logger.warning("Unable to import Paddle for GPU detection: %s", exc)
        return False


def detect_gpu_memory_mb() -> tuple[int | None, int | None]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        first_line = result.stdout.strip().splitlines()[0]
        total_raw, free_raw = [part.strip() for part in first_line.split(",", 1)]
        return int(total_raw), int(free_raw)
    except Exception:
        return None, None


def build_ocr(
    lang: str,
    device_mode: str,
    ocr_max_side_len: int,
    use_textline_orientation: bool,
        cpu_threads: int | None,
) -> PaddleOCR:
    if device_mode == "auto":
        device = "gpu:0" if detect_gpu_available() else "cpu"
    elif device_mode == "gpu":
        device = "gpu:0" if detect_gpu_available() else "cpu"
        if device == "cpu":
            logger.warning("GPU requested but unavailable. Falling back to CPU.")
    else:
        device = "cpu"

    logger.info("Initializing PaddleOCR (lang=%s, device=%s)", lang, device)

    resolved_cpu_threads = cpu_threads or max(1, min(8, os.cpu_count() or 4))
    gpu_total_mb, gpu_free_mb = detect_gpu_memory_mb() if device.startswith("gpu") else (None, None)

    primary_kwargs = {
        "lang": lang,
        "device": device,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": use_textline_orientation,
        "text_det_limit_side_len": ocr_max_side_len,
        "text_det_limit_type": "max",
    }
    if device.startswith("gpu"):
        primary_kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
        primary_kwargs["text_recognition_batch_size"] = 1
        logger.info(
            "Using GPU OCR profile with mobile detection (total=%sMB, free=%sMB).",
            gpu_total_mb,
            gpu_free_mb,
        )
        if use_textline_orientation:
            primary_kwargs["textline_orientation_batch_size"] = 4
    else:
        primary_kwargs["enable_hpi"] = False
        primary_kwargs["enable_mkldnn"] = False
        primary_kwargs["cpu_threads"] = resolved_cpu_threads
        primary_kwargs["text_recognition_batch_size"] = 4
        primary_kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
        logger.info(
            "Using CPU OCR profile with mobile detection for a stable predict() path."
        )
        if lang == "en":
            primary_kwargs["text_recognition_model_name"] = "PP-OCRv5_server_rec"
        else:
            primary_kwargs["ocr_version"] = "PP-OCRv5"
        if use_textline_orientation:
            primary_kwargs["textline_orientation_batch_size"] = 2

    def _create_ocr(target_device: str) -> PaddleOCR:
        attempts = [
            dict(primary_kwargs, device=target_device),
            {"lang": lang, "device": target_device},
            {"lang": lang, "device": target_device},
            {"lang": lang},
        ]

        for kwargs in attempts:
            try:
                return PaddleOCR(**kwargs)
            except Exception as exc:
                logger.debug("PaddleOCR init attempt failed with %s: %s", kwargs, exc)
                continue

        return PaddleOCR(lang=lang)

    return _create_ocr(device)


def collect_pdfs(input_path: Path) -> tuple[Path, list[Path], bool]:
    if input_path.is_file():
        return input_path.parent, [input_path], True

    pdfs = sorted(path for path in input_path.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")
    return input_path, pdfs, False


def normalize_predict_payload(payload: object) -> list[tuple[list[list[float]], str, float]]:
    if not isinstance(payload, dict):
        return []

    res = payload.get("res", payload)
    polygons = res.get("dt_polys") if isinstance(res, dict) else None
    texts = res.get("rec_texts") if isinstance(res, dict) else None
    scores = res.get("rec_scores") if isinstance(res, dict) else None

    if not isinstance(polygons, list) or not isinstance(texts, list):
        return []

    regions: list[tuple[list[list[float]], str, float]] = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip() or index >= len(polygons):
            continue
        poly = polygons[index]
        score = 1.0
        if isinstance(scores, list) and index < len(scores):
            try:
                score = float(scores[index])
            except Exception:
                score = 1.0
        regions.append((poly, text.strip(), score))

    return regions


def normalize_ocr_results(
    ocr_engine: PaddleOCR,
    image: np.ndarray,
) -> tuple[list[tuple[list[list[float]], str, float]], str | None]:
    regions: list[tuple[list[list[float]], str, float]] = []
    last_error: str | None = None

    try:
        if hasattr(ocr_engine, "predict"):
            for item in ocr_engine.predict(image):
                if hasattr(item, "json"):
                    regions.extend(normalize_predict_payload(item.json))
                elif isinstance(item, dict):
                    regions.extend(normalize_predict_payload(item))
    except Exception as exc:
        last_error = str(exc)
        logger.debug("predict() failed, fallback to ocr(): %s", exc)

    if regions:
        return regions, None

    try:
        raw = ocr_engine.ocr(image)
        if raw and raw[0]:
            for entry in raw[0]:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                poly = entry[0]
                text_and_score = entry[1]
                if not isinstance(text_and_score, (list, tuple)) or len(text_and_score) < 1:
                    continue
                text = str(text_and_score[0]).strip()
                if not text:
                    continue
                score = float(text_and_score[1]) if len(text_and_score) > 1 else 1.0
                regions.append((poly, text, score))
    except Exception as exc:
        last_error = str(exc)
        logger.warning("Both OCR APIs failed: %s", exc)

    return regions, last_error


def render_page_image(page: PDFPage, dpi: int, ocr_max_side_len: int) -> tuple[np.ndarray, float]:
    zoom = dpi / 72.0
    max_page_points = max(page.rect.width, page.rect.height)
    max_render_side = max_page_points * zoom

    if max_render_side > ocr_max_side_len:
        zoom *= ocr_max_side_len / max_render_side

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n).copy()

    if pix.n == 1:
        image = np.repeat(image, 3, axis=2)
    elif pix.n > 3:
        image = image[:, :, :3]

    return image, zoom


def build_image_variants(image: np.ndarray) -> list[tuple[str, np.ndarray, int]]:
    """OCR passes: contrast enhancements plus both 90-degree orientations.

    Handwritten notes are frequently scanned sideways.  PaddleOCR reads a
    rotated page far better once the image is upright, so the two quarter
    turns are tried whenever the first pass finds little text.  The third
    tuple item is the ``np.rot90`` code used to undo the rotation later.
    """
    grayscale = Image.fromarray(image).convert("L")
    enhanced = ImageOps.autocontrast(grayscale)
    enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.4, percent=160, threshold=3))
    binary_array = np.where(np.asarray(enhanced, dtype=np.uint8) >= 170, 255, 0).astype(np.uint8)
    binary = Image.fromarray(binary_array)

    return [
        ("original", image, 0),
        ("enhanced", np.array(enhanced.convert("RGB")), 0),
        ("binarized", np.array(binary.convert("RGB")), 0),
        ("rot90", np.ascontiguousarray(np.rot90(image, k=-1)), -1),
        ("rot270", np.ascontiguousarray(np.rot90(image, k=1)), 1),
    ]


def resize_image_to_max_side(image: np.ndarray, max_side: int) -> np.ndarray:
    current_height, current_width = image.shape[:2]
    current_max_side = max(current_height, current_width)
    if current_max_side <= max_side:
        return image

    scale = max_side / current_max_side
    new_width = max(1, int(round(current_width * scale)))
    new_height = max(1, int(round(current_height * scale)))

    resized = Image.fromarray(image).resize((new_width, new_height), Image.Resampling.LANCZOS)
    return np.array(resized)


def is_gpu_oom_error(error_message: str | None) -> bool:
    if not error_message:
        return False

    lowered = error_message.lower()
    return "out of memory" in lowered or "resourceexhaustederror" in lowered


def score_regions(
    regions: list[tuple[list[list[float]], str, float]],
    min_score: float,
) -> tuple[int, int, float]:
    """Rank an OCR pass by extracted characters, then lines, then confidence.

    Character count leads the tuple: a wrongly rotated page tends to produce
    many tiny fragments, so counting lines first would reward garbage.
    """
    accepted = [region for region in regions if region[2] >= min_score and region[1].strip()]
    accepted_count = len(accepted)
    total_chars = sum(len(region[1]) for region in accepted)
    average_score = round(sum(region[2] for region in accepted) / accepted_count, 4) if accepted_count else 0.0
    return (
        total_chars,
        accepted_count,
        average_score,
    )


def should_try_additional_variants(score: tuple[int, int, float]) -> bool:
    total_chars, accepted_count, average_score = score
    return accepted_count == 0 or accepted_count < 6 or total_chars < 48 or average_score < 0.82


def select_best_ocr_results(
    ocr_engine: PaddleOCR,
    image: np.ndarray,
    min_score: float,
) -> tuple[list[tuple[list[list[float]], str, float]], str, int]:
    original_regions, original_error = normalize_ocr_results(ocr_engine, image)
    any_success = original_error is None
    original_score = score_regions(original_regions, min_score)
    candidates: list[
        tuple[tuple[int, int, float], list[tuple[list[list[float]], str, float]], str, int]
    ] = [
        (original_score, original_regions, "original", 0)
    ]

    if is_gpu_oom_error(original_error):
        for max_side in (3200, 2560, 2048, 1600):
            reduced_image = resize_image_to_max_side(image, max_side)
            if reduced_image.shape == image.shape:
                continue
            reduced_regions, reduced_error = normalize_ocr_results(ocr_engine, reduced_image)
            any_success = any_success or reduced_error is None
            sx, sy = image.shape[1] / reduced_image.shape[1], image.shape[0] / reduced_image.shape[0]
            reduced_regions = [([[float(point[0]) * sx, float(point[1]) * sy] for point in poly], text, score)
                               for poly, text, score in reduced_regions]
            reduced_score = score_regions(reduced_regions, min_score)
            candidates.append((reduced_score, reduced_regions, f"reduced-{max_side}", 0))
            if reduced_score[0] > 0:
                break
            if not is_gpu_oom_error(reduced_error):
                break

    if should_try_additional_variants(original_score):
        for variant_name, variant_image, rotation in build_image_variants(image)[1:]:
            variant_regions, variant_error = normalize_ocr_results(ocr_engine, variant_image)
            any_success = any_success or variant_error is None
            candidates.append((score_regions(variant_regions, min_score), variant_regions,
                               variant_name, rotation))

    best_score, best_regions, best_variant, best_rotation = max(candidates, key=lambda item: item[0])
    if not any_success:
        raise RuntimeError("All OCR inference passes failed; source preserved for retry")
    if best_score[0] == 0:
        return original_regions, "original", 0

    return best_regions, best_variant, best_rotation


def unrotate_polygon(poly: Iterable[Iterable[float]], rotation: int,
                     shape: tuple[int, ...]) -> list[list[float]]:
    """Map OCR boxes from a rotated variant back onto the upright page image.

    ``rotation`` is the ``np.rot90`` code used to build the variant: ``-1`` is
    clockwise, ``1`` counter-clockwise, ``0`` leaves the points untouched.
    ``shape`` is the upright rendered image shape, used to flip the axis.
    """
    points = [[float(point[0]), float(point[1])] for point in poly]
    if rotation == 0 or len(points) == 0:
        return points
    height, width = shape[0], shape[1]
    if rotation == -1:  # np.rot90(image, k=-1): rotated[y, x] = image[H-1-x, y]
        return [[y, height - 1 - x] for x, y in points]
    # np.rot90(image, k=1): rotated[y, x] = image[y, W-1-x]
    return [[width - 1 - y, x] for x, y in points]


def page_has_native_text(page: PDFPage) -> bool:
    try:
        native_text = " ".join(page.get_text("text").split())
    except Exception:
        return False

    # Count visible characters only: a title page with a few glyphs and wide
    # spacing must not look "text rich" and bypass OCR.
    clean_text = "".join(char for char in native_text
                         if char.isprintable() and not char.isspace()).strip()
    return len(clean_text) >= 50


def page_is_monochrome(page: PDFPage) -> bool:
    """Detect a genuinely featureless page before initializing OCR."""
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(1.0, 1.0),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    samples = pixmap.samples
    return bool(samples) and samples.count(samples[0]) == len(samples)


def polygon_to_rect(poly: Iterable[Iterable[float]], zoom: float) -> PDFRect | None:
    points = list(poly)
    if not points:
        return None

    xs: list[float] = []
    ys: list[float] = []

    for point in points:
        coords = list(point)
        if len(coords) < 2:
            continue
        xs.append(float(coords[0]) / zoom)
        ys.append(float(coords[1]) / zoom)

    if not xs or not ys:
        return None

    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    if x1 - x0 < 1 or y1 - y0 < 1:
        return None

    return fitz.Rect(x0, y0, x1, y1)


def add_invisible_text(page: PDFPage, rect: PDFRect, text: str) -> bool:
    if not text.strip():
        return False
    font_args = {}
    unicode_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if any(ord(char) > 255 for char in text) and unicode_font.is_file():
        # Standard PDF Helvetica cannot preserve Greek/mathematical OCR text.
        font_args = {"fontname": "OCRUnicode", "fontfile": str(unicode_font)}

    font_sizes = [
        max(4.0, min(rect.height * 0.9, 24.0)),
        max(4.0, min(rect.height * 0.75, 18.0)),
        max(4.0, min(rect.height * 0.6, 14.0)),
    ]

    for font_size in font_sizes:
        inserted = page.insert_textbox(
            rect,
            text,
            fontsize=font_size,
            render_mode=3,
            overlay=False,
            **font_args,
        )
        if inserted >= 0:
            return True

    baseline = fitz.Point(rect.x0, max(rect.y1 - 1, rect.y0 + 1))
    page.insert_text(
        baseline,
        text,
        fontsize=font_sizes[-1],
        render_mode=3,
        overlay=False,
        **font_args,
    )

    return True


def compress_pdf_losslessly(source: Path, destination: Path, expected_pages: int) -> None:
    """Re-serialize a PDF with qpdf stream/object compression and verify structure."""
    import pikepdf

    with pikepdf.Pdf.open(source) as pdf:
        if len(pdf.pages) != expected_pages:
            raise RuntimeError(f"Page count changed before compression: {len(pdf.pages)}")
        pdf.save(
            destination,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    with pikepdf.Pdf.open(destination) as compressed:
        if len(compressed.pages) != expected_pages:
            raise RuntimeError("Page count changed during lossless compression.")
        errors = [
            issue
            for issue in compressed.check_pdf_syntax()
            if "error" in str(issue).lower()
        ]
        if errors:
            raise RuntimeError(f"Compressed PDF failed syntax checks: {errors[:3]}")


def process_pdf(
    pdf_path: Path,
    out_path: Path,
    get_ocr_engine: Callable[[], PaddleOCR],
    dpi: int,
    ocr_max_side_len: int,
    min_score: float,
    force_ocr: bool,
) -> None:
    """OCR only sparse-text pages, preserve originals, and publish verified output atomically."""
    start = time.time()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.error("Failed to open %s: %s", pdf_path, exc)
        return

    total_regions = 0
    native_pages = 0
    ocr_pages = 0
    blank_pages = 0
    unicode_ocr = False
    # Identical template/diagram renders recur across pages and notes. Cache
    # regions by rendered bytes + processing profile, never by page number.
    visual_cache = Path(__file__).resolve().parent / "ocr_visual_cache"
    visual_cache.mkdir(exist_ok=True)
    profile = hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + str(
        getattr(get_ocr_engine, "cache_profile", "en-default")) + str(min_score) + "rot=1"

    source_page_count = len(doc)
    with tempfile.TemporaryDirectory(prefix=f".{out_path.stem}.", dir=out_path.parent) as temp_dir:
        temp_root = Path(temp_dir)
        fitz_temp = temp_root / "fitz-output.pdf"
        compressed_temp = temp_root / "qpdf-output.pdf"
        try:
            for page_index, page in enumerate(doc, start=1):
                if not force_ocr and page_has_native_text(page):
                    native_pages += 1
                    logger.info("%s page %d: kept native PDF text layer", pdf_path.name, page_index)
                    continue
                if not force_ocr and page_is_monochrome(page):
                    blank_pages += 1
                    logger.info("%s page %d: skipped featureless page", pdf_path.name, page_index)
                    continue

                image, zoom = render_page_image(page, dpi, ocr_max_side_len)
                image_key = hashlib.sha256(profile.encode() + str(image.shape).encode() + image.tobytes()).hexdigest()
                cached_regions = visual_cache / f"{image_key}.json"
                cached = None
                if not force_ocr and cached_regions.is_file():
                    try:
                        cached = json.loads(cached_regions.read_text())
                    except (ValueError, OSError):
                        pass
                if isinstance(cached, dict) and isinstance(cached.get("regions"), list):
                    regions = cached["regions"]
                    rotation = int(cached.get("rotation", 0))
                    variant_name = "content-cache"
                else:
                    regions, variant_name, rotation = select_best_ocr_results(
                        get_ocr_engine(), image, min_score)
                    # Bound only regenerable OCR metadata, not original page/PDF data.
                    cache_bytes = sum(p.stat().st_size for p in visual_cache.glob("*.json"))
                    if cache_bytes < 32 * 1024**2:
                        payload = json.dumps({"regions": regions, "rotation": rotation},
                                             default=lambda value: value.tolist() if hasattr(value, "tolist") else float(value))
                        temporary = cached_regions.with_suffix(f".{os.getpid()}.tmp")
                        temporary.write_text(payload)
                        os.replace(temporary, cached_regions)
                page_inserted = 0
                for poly, text, score in regions:
                    if score < min_score:
                        continue
                    rect = polygon_to_rect(unrotate_polygon(poly, rotation, image.shape), zoom)
                    if rect is not None and add_invisible_text(page, rect, text):
                        page_inserted += 1
                        unicode_ocr = unicode_ocr or any(ord(char) > 255 for char in text)

                ocr_pages += 1
                total_regions += page_inserted
                logger.info(
                    "%s page %d: inserted %d text regions (%s pass)",
                    pdf_path.name,
                    page_index,
                    page_inserted,
                    variant_name,
                )

            if unicode_ocr:
                doc.subset_fonts()
            doc.save(fitz_temp, garbage=4, deflate=True, clean=True)
        finally:
            doc.close()

        compress_pdf_losslessly(fitz_temp, compressed_temp, source_page_count)
        os.replace(compressed_temp, out_path)
    logger.info(
        "Done %s -> %s | ocr_pages=%d | native_pages=%d | blank_pages=%d | regions=%d | bytes=%d->%d | %.2fs",
        pdf_path,
        out_path,
        ocr_pages,
        native_pages,
        blank_pages,
        total_regions,
        pdf_path.stat().st_size,
        out_path.stat().st_size,
        time.time() - start,
    )


def ensure_output_dir(requested_output_dir: Path) -> Path:
    try:
        requested_output_dir.mkdir(parents=True, exist_ok=True)
        return requested_output_dir
    except PermissionError:
        if requested_output_dir.is_absolute():
            fallback = Path.cwd() / requested_output_dir.relative_to(requested_output_dir.anchor)
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(
                "No permission to create %s. Falling back to %s",
                requested_output_dir,
                fallback,
            )
            return fallback
        raise


def determine_output_dir(input_path: Path, requested_output_dir: Path | None) -> Path:
    if requested_output_dir is not None:
        return ensure_output_dir(requested_output_dir.expanduser().resolve())

    if input_path.is_file():
        suggested = input_path.parent / f"{input_path.stem}_searchable"
    else:
        suggested = input_path.parent / f"{input_path.name}_searchable"

    return ensure_output_dir(suggested)


def build_output_path(pdf_path: Path, input_root: Path, output_dir: Path, single_file: bool) -> Path:
    relative_path = Path(pdf_path.name) if single_file else pdf_path.relative_to(input_root)
    out_path = output_dir / relative_path

    if out_path.resolve() == pdf_path.resolve():
        return out_path.with_name(f"{out_path.stem}_searchable{out_path.suffix}")

    return out_path


def main() -> None:
    args = parse_args()
    input_path = resolve_input_path(args.input_path)
    output_dir = determine_output_dir(input_path, args.output_dir)
    input_root, pdfs, single_file = collect_pdfs(input_path)

    if BOOTSTRAPPED_PATHS:
        logger.info(
            "Using custom Python library roots (%s): %s",
            os.environ.get("SAM_PDF_PADDLE_VARIANT", "auto"),
            ", ".join(BOOTSTRAPPED_PATHS),
        )

    if not pdfs:
        raise SystemExit(f"No PDF files found under: {input_path}")

    ocr_engine: PaddleOCR | None = None

    def get_ocr_engine() -> PaddleOCR:
        nonlocal ocr_engine

        if ocr_engine is None:
            ocr_engine = build_ocr(
                lang=args.lang,
                device_mode=args.device,
                ocr_max_side_len=args.ocr_max_side_len,
                use_textline_orientation=args.use_textline_orientation,
                cpu_threads=args.cpu_threads,
            )

        return ocr_engine

    get_ocr_engine.cache_profile = f"{args.lang}:mobile-det:server-rec:orientation={args.use_textline_orientation}"

    logger.info("Found %d PDF file(s) in %s", len(pdfs), input_path)

    for pdf_path in pdfs:
        out_path = build_output_path(pdf_path, input_root, output_dir, single_file)
        process_pdf(
            pdf_path,
            out_path,
            get_ocr_engine,
            dpi=args.dpi,
            ocr_max_side_len=args.ocr_max_side_len,
            min_score=args.min_score,
            force_ocr=args.force_ocr,
        )

    logger.info("All done. Output root: %s", output_dir)


if __name__ == "__main__":
    main()
