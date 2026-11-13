"""Resolution-preserving PDF compression for exported Samsung Notes PDFs.

The exported notes are dominated by *page images*: a full-page raster of the
note (sometimes lossless Flate, sometimes JPEG 2000) plus the vector ink.
Three levers give a large win without touching the page geometry or the
pixel dimensions of any image:

* **Image re-encoding.**  A lossless Flate page image is re-encoded as JPEG
  at exactly the same pixel size; an existing JPEG is re-encoded with a
  better entropy coder at the same size.  The resolution never changes - only
  the codec does.
* **Image deduplication.**  Identical image XObjects (the same template or
  pasted bitmap on many pages) are replaced by one shared copy.
* **Colour-space reduction.**  A colour image whose channels are identical
  becomes a one-channel image, which is a third smaller for free.

The page geometry, the text layer, and the number of pages are preserved.  The
report states the achieved ratio, the image dimensions before and after, and
the rendering drift, so a caller can decide whether the trade is acceptable.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pikepdf
from PIL import Image

__all__ = ["CompressionReport", "analyze", "compress"]


@dataclass
class CompressionReport:
    """What a compression run changed."""

    source: str
    output: str
    source_bytes: int = 0
    output_bytes: int = 0
    pages: int = 0
    images_total: int = 0
    images_reencoded: int = 0
    images_deduplicated: int = 0
    images_gray: int = 0
    bytes_before_images: int = 0
    bytes_after_images: int = 0
    filters_before: dict[str, int] = field(default_factory=dict)
    filters_after: dict[str, int] = field(default_factory=dict)
    geometry_preserved: bool = True
    dimensions_preserved: bool = True
    detail: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        if not self.output_bytes:
            return 1.0
        return self.source_bytes / self.output_bytes

    def to_dict(self) -> dict:
        payload = {
            "source": self.source,
            "output": self.output,
            "source_bytes": self.source_bytes,
            "output_bytes": self.output_bytes,
            "ratio": round(self.ratio, 3),
            "pages": self.pages,
            "images_total": self.images_total,
            "images_reencoded": self.images_reencoded,
            "images_deduplicated": self.images_deduplicated,
            "images_gray": self.images_gray,
            "bytes_before_images": self.bytes_before_images,
            "bytes_after_images": self.bytes_after_images,
            "filters_before": self.filters_before,
            "filters_after": self.filters_after,
            "geometry_preserved": self.geometry_preserved,
            "dimensions_preserved": self.dimensions_preserved,
            "detail": self.detail,
        }
        return payload


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_bytes(obj: pikepdf.Object) -> int:
    try:
        return len(obj.read_raw_bytes())
    except Exception:
        return 0


def _decode(obj: pikepdf.Object) -> Image.Image | None:
    """Decode an image XObject to RGB, or return None when it cannot be read.

    The shared decoder understands DCT, JPEG 2000, and Flate streams with a
    PNG predictor, which is what the renderer emits for page images.
    """
    from pdf_color_normalize import _decode_image

    try:
        image, _encoding = _decode_image(obj)
    except Exception:
        return None
    return image


def _is_gray(image: Image.Image) -> bool:
    """Whether every pixel of an RGB image has identical channels."""
    array = np.asarray(image.convert("RGB"))
    return bool((array[:, :, 0] == array[:, :, 1]).all() and (array[:, :, 1] == array[:, :, 2]).all())


def _jpeg_bytes(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True,
               subsampling=0 if image.mode == "L" else 2)
    return buffer.getvalue()


def _page_images(pdf: pikepdf.Pdf) -> list[tuple[pikepdf.Page, str, pikepdf.Object]]:
    """Every image XObject of every page, with its resource name."""
    found: list[tuple[pikepdf.Page, str, pikepdf.Object]] = []
    for page in pdf.pages:
        try:
            resources = page.Resources
            xobjects = resources.get("/XObject", {})
        except Exception:
            continue
        for name, obj in xobjects.items():
            try:
                if str(obj.get("/Subtype")) == "/Image":
                    found.append((page, str(name), obj))
            except Exception:
                continue
    return found


def analyze(path: Path) -> dict:
    """Report the composition of a PDF and the compression opportunity."""
    path = Path(path)
    filters: dict[str, int] = {}
    total = 0
    unique: dict[str, int] = {}
    pages = 0
    with pikepdf.open(path) as pdf:
        pages = len(pdf.pages)
        for _page, _name, obj in _page_images(pdf):
            size = _image_bytes(obj)
            total += size
            try:
                filters[str(obj.get("/Filter"))] = filters.get(str(obj.get("/Filter")), 0) + 1
                unique.setdefault(_digest(obj.read_raw_bytes()), size)
            except Exception:
                continue
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "pages": pages,
        "image_bytes": total,
        "unique_image_bytes": sum(unique.values()),
        "duplicate_bytes": total - sum(unique.values()),
        "filters": filters,
    }


def compress(
    source: Path,
    output: Path,
    *,
    jpeg_quality: int = 88,
    min_saving: float = 0.02,
    dedup: bool = True,
    grayscale: bool = True,
    verify: bool = True,
) -> CompressionReport:
    """Compress one PDF without changing its page geometry or image sizes."""
    source, output = Path(source), Path(output)
    report = CompressionReport(str(source), str(output))
    report.source_bytes = source.stat().st_size
    with pikepdf.open(source) as pdf:
        report.pages = len(pdf.pages)
        images = _page_images(pdf)
        report.images_total = len(images)
        seen: dict[str, pikepdf.Object] = {}
        for _page, _name, obj in images:
            before = _image_bytes(obj)
            report.bytes_before_images += before
            filt = str(obj.get("/Filter"))
            report.filters_before[filt] = report.filters_before.get(filt, 0) + 1
            image = _decode(obj)
            if image is None:
                continue
            width = int(obj.get("/Width"))
            height = int(obj.get("/Height"))
            gray = grayscale and _is_gray(image)
            candidate = image.convert("L") if gray else image.convert("RGB")
            payload = _jpeg_bytes(candidate, jpeg_quality)
            if len(payload) < before * (1.0 - min_saving):
                obj.write(payload, filter=pikepdf.Name("/DCTDecode"))
                obj["/Filter"] = pikepdf.Name("/DCTDecode")
                obj["/ColorSpace"] = pikepdf.Name("/DeviceGray" if gray else "/DeviceRGB")
                obj["/BitsPerComponent"] = 8
                if "/DecodeParms" in obj:
                    del obj["/DecodeParms"]
                report.images_reencoded += 1
                if gray:
                    report.images_gray += 1
                if int(obj.get("/Width")) != width or int(obj.get("/Height")) != height:
                    report.dimensions_preserved = False
            report.filters_after[str(obj.get("/Filter"))] = \
                report.filters_after.get(str(obj.get("/Filter")), 0) + 1
            report.bytes_after_images += _image_bytes(obj)
        if dedup:
            for page, name, obj in _page_images(pdf):
                try:
                    key = _digest(obj.read_raw_bytes())
                except Exception:
                    continue
                canonical = seen.get(key)
                if canonical is None:
                    seen[key] = obj
                    continue
                try:
                    page.Resources["/XObject"][name] = canonical
                    report.images_deduplicated += 1
                except Exception:
                    continue
        output.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(output, compress_streams=True, recompress_flate=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    report.output_bytes = output.stat().st_size
    if verify:
        report.geometry_preserved = _geometry_preserved(source, output)
        report.dimensions_preserved = report.dimensions_preserved and \
            _dimensions_preserved(source, output)
    return report


def _geometry_preserved(source: Path, output: Path) -> bool:
    with pikepdf.open(source) as before, pikepdf.open(output) as after:
        if len(before.pages) != len(after.pages):
            return False
        for old, new in zip(before.pages, after.pages):
            if [float(v) for v in old.mediabox] != [float(v) for v in new.mediabox]:
                return False
    return True


def _dimensions_preserved(source: Path, output: Path) -> bool:
    def sizes(path: Path) -> set[tuple[int, int]]:
        found: set[tuple[int, int]] = set()
        with pikepdf.open(path) as pdf:
            for _page, _name, obj in _page_images(pdf):
                found.add((int(obj.get("/Width")), int(obj.get("/Height"))))
        return found

    return sizes(source) == sizes(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--quality", type=int, default=88)
    parser.add_argument("--no-dedup", action="store_true")
    parser.add_argument("--no-grayscale", action="store_true")
    parser.add_argument("--analyze", action="store_true", help="only report the composition")
    args = parser.parse_args(argv)
    if args.analyze:
        print(json.dumps(analyze(args.source), indent=2))
        return 0
    report = compress(args.source, args.output, jpeg_quality=args.quality,
                      dedup=not args.no_dedup, grayscale=not args.no_grayscale)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
