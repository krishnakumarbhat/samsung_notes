"""Light-canvas normalization for Samsung Notes PDF exports.

Samsung Notes stores a page background and pen colours separately.  A legacy
export can therefore contain a full-page dark fill followed by otherwise valid
vector ink, or a dark raster image with light ink baked into it.  Rendering such
a page faithfully is not useful for reading, OCR, or printing: the paper is
effectively hidden.

This module implements the same colour transform used by ``sdocx``'s light
theme (reverse HSL lightness while preserving hue), plus two narrowly scoped
PDF repairs:

* replace the single full-page background fill with white and re-tune every
  other colour operator in the page content stream for the white canvas (the
  variant with the better WCAG contrast against white wins, so bright ink is
  darkened while genuinely dark ink stays dark);
* reverse the pixels of a genuinely dark image XObject, re-encoding it with the
  same dimensions.

Only pages whose background or image is actually dark are changed.  Unrelated
light pages, embedded source PDFs, text objects, and vector operators are left
alone.  The module never writes source data; callers provide an output path and
verify the result before publishing.
"""

from __future__ import annotations

import io
import re
import shutil
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image

__all__ = [
    "PageReport",
    "PdfReport",
    "adapt_gray",
    "adapt_rgb",
    "inspect_page",
    "normalize_content_stream",
    "normalize_pdf",
    "render_page_stats",
    "reverse_rgb",
    "strip_background_stream",
]

# PDF numbers used by the renderer include leading-dot decimals (``.0039``).
_NUM = rb"(?:\d+(?:\.\d+)?|\.\d+)"
_RECT = (
    rb"(?P<rect>"
    + _NUM
    + rb"\s+"
    + _NUM
    + rb"\s+m\s+"
    + _NUM
    + rb"\s+"
    + _NUM
    + rb"\s+l\s+"
    + _NUM
    + rb"\s+"
    + _NUM
    + rb"\s+l\s+0\s+"
    + _NUM
    + rb"\s+l\s*h\s+f\*?)"
)
_PRE = rb"(?P<pre>(?:q\s+" + _NUM + rb"\s+0\s+0\s+-" + _NUM + rb"\s+0\s+" + _NUM + rb"\s+cm\s+)?)"
_BG_GRAY = re.compile(_PRE + rb"(?P<color>" + _NUM + rb")\s+g\s+" + _RECT, re.S)
_BG_RGB = re.compile(
    _PRE + rb"(?P<color>" + _NUM + rb"\s+" + _NUM + rb"\s+" + _NUM + rb")\s+rg\s+" + _RECT,
    re.S,
)
_COLOR_OP = re.compile(
    rb"(?P<v>" + _NUM + rb")(?P<rgb>\s+" + _NUM + rb"\s+" + _NUM + rb")?\s+"
    rb"(?P<op>g|G|rg|RG)(?![A-Za-z])"
)
# A value outside the PDF colour range marks an inserted white background so
# the colour-reversal pass cannot invert it.  It is restored before returning.
_WHITE_GRAY = b"1.0000001"
_WHITE_RGB = b"1.0000001 1.0000001 1.0000001"


@dataclass
class PageReport:
    """Per-page repair facts for an audit or verification report."""

    index: int
    repaired: bool = False
    background_fills: int = 0
    color_operators: int = 0
    images: int = 0
    before_mean: float | None = None
    after_mean: float | None = None
    text_chars: int = 0
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PdfReport:
    """Document-level repair result."""

    source: str
    output: str
    pages: int
    repaired_pages: list[int] = field(default_factory=list)
    before_black_pages: list[int] = field(default_factory=list)
    after_black_pages: list[int] = field(default_factory=list)
    page_reports: list[PageReport] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        return payload


def reverse_rgb(r: float, g: float, b: float) -> tuple[int, int, int]:
    """Reverse HSL lightness for an RGB colour, preserving hue.

    The implementation mirrors ``sdocx``'s ``reverse_color``: colours whose
    HSL lightness lies in the neutral ``0.4..0.6`` band are left unchanged;
    otherwise every channel is shifted by ``255 - min - max``.  Inputs may be
    either 0..1 PDF floats or 0..255 bytes.
    """
    values = [r, g, b]
    scale = 1.0 if max(values) <= 1.0 else 255.0
    channels = [max(0.0, min(1.0, float(v) / scale)) * 255.0 for v in values]
    low, high = min(channels), max(channels)
    lightness = (low + high) / 2.0
    if 102.0 <= lightness <= 153.0:
        return tuple(int(round(v)) for v in channels)  # type: ignore[return-value]
    offset = 255.0 - low - high
    return tuple(int(round(max(0.0, min(255.0, v + offset)))) for v in channels)  # type: ignore[return-value]


def _relative_luminance(rgb: Sequence[float]) -> float:
    """WCAG relative luminance for 0..255 channels."""
    channels = []
    for value in rgb:
        scaled = max(0.0, min(255.0, float(value))) / 255.0
        channels.append(scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_on_white(rgb: Sequence[float]) -> float:
    """WCAG contrast ratio of a colour against a white page."""
    return 1.05 / (_relative_luminance(rgb) + 0.05)


def _offset_variant(rgb: Sequence[float]) -> tuple[int, int, int]:
    """Shift every channel by ``255 - min - max`` (a pure lightness flip)."""
    low, high = min(rgb), max(rgb)
    offset = 255.0 - low - high
    return tuple(int(round(max(0.0, min(255.0, value + offset)))) for value in rgb)  # type: ignore[return-value]


def adapt_rgb(r: float, g: float, b: float) -> tuple[int, int, int]:
    """Choose the colour variant that reads best on a white canvas.

    Ink drawn on a dark canvas is usually bright; moved onto white paper the
    same hue has to be darkened, while genuinely dark ink (which was simply
    invisible on black) must stay dark.  Both candidates are compared with the
    WCAG contrast ratio against white and the more readable one wins.
    """
    values = [r, g, b]
    scale = 1.0 if max(values) <= 1.0 else 255.0
    base = [max(0.0, min(1.0, float(v) / scale)) * 255.0 for v in values]
    candidate = _offset_variant(base)
    if _contrast_on_white(candidate) > _contrast_on_white(base) + 1e-9:
        return candidate  # type: ignore[return-value]
    return tuple(int(round(v)) for v in base)  # type: ignore[return-value]


def adapt_gray(value: float) -> float:
    """Grayscale counterpart of :func:`adapt_rgb` for 0..255 input."""
    base = max(0.0, min(255.0, float(value)))
    candidate = 255.0 - base
    if _contrast_on_white((candidate, candidate, candidate)) > _contrast_on_white((base, base, base)) + 1e-9:
        return candidate
    return base


def _color_luminance(values: Sequence[float]) -> float:
    if len(values) == 1:
        return values[0] * 255.0
    return 0.299 * values[0] * 255.0 + 0.587 * values[1] * 255.0 + 0.114 * values[2] * 255.0


def _split_numbers(value: bytes) -> list[float]:
    return [float(part) for part in value.split()]


@dataclass
class BackgroundFill:
    """A fill that covers the whole page (the page background)."""

    color_start: int
    color_end: int
    kind: str
    values: tuple[float, ...]
    span_start: int
    span_end: int

    @property
    def dark(self) -> bool:
        return _color_luminance(self.values) < 30.0


def _tokens(data: bytes):
    """Yield ``(start, end, kind, value)`` for every content-stream token.

    The scanner is defensive: malformed content (an unbalanced bracket or an
    unterminated string) must never swallow the rest of the stream, so every
    skip is bounded and falls back to treating the byte as ordinary.
    """
    index = 0
    size = len(data)
    string_limit = 64 * 1024
    array_limit = 4 * 1024 * 1024
    while index < size:
        char = data[index]
        if char in b" \t\r\n\f\x00":
            index += 1
            continue
        if char == 0x25:  # '%' comment
            end = data.find(b"\n", index)
            index = size if end < 0 else end + 1
            continue
        start = index
        if char == 0x28:  # '(' literal string
            cursor = index + 1
            closed = False
            while cursor < size and cursor - index <= string_limit:
                current = data[cursor]
                if current == 0x5C:  # backslash escape
                    cursor += 2
                    continue
                if current == 0x29:
                    cursor += 1
                    closed = True
                    break
                cursor += 1
            if closed:
                index = cursor
                yield start, index, "string", b""
                continue
            index += 1
            continue
        if char == 0x3C:  # '<' hex string or dictionary
            if index + 1 < size and data[index + 1] == 0x3C:
                cursor = index + 2
                depth = 1
                closed = False
                while cursor < size and cursor - index <= array_limit:
                    if data[cursor] == 0x3C and cursor + 1 < size and data[cursor + 1] == 0x3C:
                        depth += 1
                        cursor += 2
                        continue
                    if data[cursor] == 0x3E and cursor + 1 < size and data[cursor + 1] == 0x3E:
                        depth -= 1
                        cursor += 2
                        if depth == 0:
                            closed = True
                            break
                        continue
                    cursor += 1
                if closed:
                    index = cursor
                    yield start, index, "skip", b""
                    continue
                index += 1
                continue
            end = data.find(b">", index)
            if end < 0:
                index += 1
                continue
            index = end + 1
            yield start, index, "string", b""
            continue
        if char in b"[]{}":
            cursor = index + 1
            depth = 1
            closed = False
            while cursor < size and cursor - index <= array_limit:
                current = data[cursor]
                if current == 0x28:  # literal string inside an array
                    scan = cursor + 1
                    while scan < size and scan - cursor <= string_limit:
                        if data[scan] == 0x5C:
                            scan += 2
                            continue
                        if data[scan] == 0x29:
                            scan += 1
                            break
                        scan += 1
                    if scan > cursor + string_limit:
                        break
                    cursor = scan
                    continue
                if current == 0x3C:  # hex string inside an array
                    end = data.find(b">", cursor)
                    if end < 0:
                        break
                    cursor = end + 1
                    continue
                if current in b"[{":
                    depth += 1
                elif current in b"]}":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        closed = True
                        break
                cursor += 1
            if closed:
                index = cursor
                yield start, index, "skip", b""
                continue
            index += 1
            continue
        if char == 0x2F:  # '/' name
            index += 1
            while index < size and data[index] not in b" \t\r\n\f\x00/[]()<>{}":
                index += 1
            yield start, index, "name", data[start:index]
            continue
        if char in b"+-.0123456789":
            index += 1
            while index < size and data[index] in b"+-.0123456789eE":
                index += 1
            yield start, index, "number", data[start:index]
            continue
        if char in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ*'\"":
            index += 1
            while index < size and data[index] in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789*'\"":
                index += 1
            yield start, index, "op", data[start:index]
            continue
        index += 1


def _compose(current: tuple[float, ...], extra: tuple[float, ...]) -> tuple[float, ...]:
    """Multiply two 2D affine matrices (``cm`` operands)."""
    a, b, c, d, e, f = current
    a2, b2, c2, d2, e2, f2 = extra
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def _transform_point(ctm: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = ctm
    return (a * x + c * y + e, b * x + d * y + f)


def background_fills(
    data: bytes,
    page_width: float | None = None,
    page_height: float | None = None,
) -> list[BackgroundFill]:
    """Find fills that paint the whole page.

    The content stream is scanned with a small interpreter that tracks the
    current transformation matrix and the current path, so both the renderer's
    ``m``/``l``/``h`` backgrounds and the ``re`` backgrounds of embedded source
    documents are recognised.  Without a page size the geometric test is
    skipped and only the colour test applies.
    """
    if not data:
        return []
    ctm: tuple[float, ...] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack: list[tuple[float, ...]] = []
    operands: list[float] = []
    number_spans: list[tuple[int, int]] = []
    path_bbox: list[float] | None = None
    path_start: int | None = None
    color: tuple[int, int, str, tuple[float, ...]] | None = None
    fills: list[BackgroundFill] = []
    for start, end, kind, value in _tokens(data):
        if kind == "number":
            try:
                operands.append(float(value))
            except ValueError:
                operands.append(0.0)
            number_spans.append((start, end))
            if len(number_spans) > 8:
                number_spans.pop(0)
            continue
        if kind == "op":
            operator = value.decode("latin-1")
            if operator == "q":
                stack.append(ctm)
            elif operator == "Q":
                if stack:
                    ctm = stack.pop()
            elif operator == "cm" and len(operands) >= 6:
                ctm = _compose(ctm, tuple(operands[-6:]))
            elif operator == "m" and len(operands) >= 2:
                x, y = operands[-2], operands[-1]
                path_bbox = [x, y, x, y]
                path_start = start
            elif operator == "l" and len(operands) >= 2:
                x, y = operands[-2], operands[-1]
                if path_bbox is None:
                    path_bbox = [x, y, x, y]
                    path_start = start
                else:
                    path_bbox[0] = min(path_bbox[0], x)
                    path_bbox[1] = min(path_bbox[1], y)
                    path_bbox[2] = max(path_bbox[2], x)
                    path_bbox[3] = max(path_bbox[3], y)
            elif operator in {"c", "v", "y"} and len(operands) >= 2:
                for index in range(0, len(operands) - 1, 2):
                    x, y = operands[index], operands[index + 1]
                    if path_bbox is None:
                        path_bbox = [x, y, x, y]
                        path_start = start
                    else:
                        path_bbox[0] = min(path_bbox[0], x)
                        path_bbox[1] = min(path_bbox[1], y)
                        path_bbox[2] = max(path_bbox[2], x)
                        path_bbox[3] = max(path_bbox[3], y)
            elif operator == "re" and len(operands) >= 4:
                x, y, width, height = operands[-4:]
                path_bbox = [x, y, x + width, y + height]
                path_start = start
            elif operator == "g" and operands and number_spans:
                span = number_spans[-1]
                color = (span[0], span[1], "g", (operands[-1],))
            elif operator == "rg" and len(operands) >= 3 and len(number_spans) >= 3:
                spans = number_spans[-3:]
                color = (spans[0][0], spans[-1][1], "rg", tuple(operands[-3:]))
            elif operator in {"k", "sc", "scn"}:
                color = None
            elif operator in {"f", "f*", "F", "b", "b*", "B", "B*"}:
                if color is not None and path_bbox is not None:
                    if _covers_page(path_bbox, ctm, page_width, page_height):
                        span_start = min(path_start if path_start is not None else start, color[0])
                        fills.append(BackgroundFill(
                            color_start=color[0], color_end=color[1], kind=color[2],
                            values=color[3], span_start=span_start, span_end=end,
                        ))
                path_bbox = None
                path_start = None
            elif operator == "n":
                path_bbox = None
                path_start = None
            operands = []
            continue
        operands = []
    return fills


def _covers_page(
    bbox: Sequence[float],
    ctm: tuple[float, ...],
    page_width: float | None,
    page_height: float | None,
) -> bool:
    """Whether a path, after the current transform, covers the whole page."""
    if page_width is None or page_height is None:
        return True
    a, b, c, d, _e, _f = ctm
    if abs(b) > 1e-6 or abs(c) > 1e-6:  # rotated or sheared pages are skipped
        return False
    x0, y0, x1, y1 = bbox
    corners = [
        _transform_point(ctm, x0, y0),
        _transform_point(ctm, x1, y0),
        _transform_point(ctm, x1, y1),
        _transform_point(ctm, x0, y1),
    ]
    width = max(point[0] for point in corners) - min(point[0] for point in corners)
    height = max(point[1] for point in corners) - min(point[1] for point in corners)
    if width <= 0 or height <= 0:
        return False
    return (
        width >= 0.85 * float(page_width)
        and height >= 0.85 * float(page_height)
    )


def _regex_background_fills(data: bytes) -> list[BackgroundFill]:
    """Fallback background detection for streams without a page size."""
    fills: list[BackgroundFill] = []
    for pattern in (_BG_GRAY, _BG_RGB):
        for match in pattern.finditer(data):
            color = _split_numbers(match.group("color"))
            if _color_luminance(color) >= 30.0:
                continue
            fills.append(BackgroundFill(
                color_start=match.start("color"),
                color_end=match.end("color"),
                kind="g" if len(color) == 1 else "rg",
                values=tuple(color),
                span_start=match.start(),
                span_end=match.end(),
            ))
    fills.sort(key=lambda fill: fill.color_start)
    return fills


def has_dark_background(data: bytes, page_size: tuple[float, float] | None = None) -> bool:
    """Return whether a stream contains a dark full-page background fill."""
    if page_size is not None:
        return any(fill.dark for fill in background_fills(data, page_size[0], page_size[1]))
    return bool(_regex_background_fills(data))


def strip_background_stream(
    data: bytes,
    page_size: tuple[float, float] | None = None,
) -> tuple[bytes, int]:
    """Remove full-page background fills from a content stream.

    The surrounding ``q``/``cm``/``Q`` operators are retained so the stream
    stack stays balanced; only the colour, the path, and the fill operator are
    removed.  Used to turn a rendered dark page into a transparent ink layer.
    """
    if page_size is not None:
        fills = background_fills(data, page_size[0], page_size[1])
    else:
        fills = _regex_background_fills(data)
    if not fills:
        return data, 0
    pieces: list[bytes] = []
    cursor = 0
    removed = 0
    for fill in fills:
        start, end = fill.span_start, fill.span_end
        if start < cursor:
            continue
        pieces.append(data[cursor:start])
        cursor = end
        removed += 1
    pieces.append(data[cursor:])
    return b"".join(pieces), removed


def _substitute_outside(
    data: bytes,
    pattern: re.Pattern[bytes],
    handler,
    protected: list[tuple[int, int]],
) -> bytes:
    """Apply ``pattern`` while skipping matches that touch protected spans."""
    pieces: list[bytes] = []
    cursor = 0
    for match in pattern.finditer(data):
        if any(match.start() < end and start < match.end() for start, end in protected):
            continue
        pieces.append(data[cursor:match.start()])
        pieces.append(handler(match))
        cursor = match.end()
    pieces.append(data[cursor:])
    return b"".join(pieces)


def normalize_content_stream(
    data: bytes,
    *,
    strip_background: bool = False,
    reverse_colors: bool = True,
    page_size: tuple[float, float] | None = None,
) -> tuple[bytes, dict]:
    """Whiten dark page backgrounds and re-tune ink colours for white paper.

    Returns ``(content, facts)`` where ``facts`` contains counts of background
    fills and colour operators changed.  ``strip_background=True`` removes the
    background instead of replacing it with white (for transparent overlays).
    ``reverse_colors=False`` is useful for a dry structural inspection.

    A page background that is already light is preserved: only the *dark*
    background is replaced, and neither background takes part in the colour
    re-tuning, so a white paper background never turns black.
    """
    facts = {"background_fills": 0, "color_operators": 0}
    if page_size is not None:
        fills = background_fills(data, page_size[0], page_size[1])
    else:
        fills = _regex_background_fills(data)
    protected: list[tuple[int, int]] = []
    if fills:
        pieces: list[bytes] = []
        cursor = 0
        offset = 0
        for fill in fills:
            if fill.color_start < cursor:
                continue
            if strip_background:
                pieces.append(data[cursor:fill.span_start])
                offset += fill.span_start - cursor
                cursor = fill.span_end
                facts["background_fills"] += 1
                continue
            pieces.append(data[cursor:fill.color_start])
            offset += fill.color_start - cursor
            if fill.dark:
                placeholder = _WHITE_GRAY if fill.kind == "g" else _WHITE_RGB
                pieces.append(placeholder)
                protected.append((offset, offset + len(placeholder)))
                offset += len(placeholder)
                facts["background_fills"] += 1
            else:
                size = fill.color_end - fill.color_start
                pieces.append(data[fill.color_start:fill.color_end])
                protected.append((offset, offset + size))
                offset += size
            cursor = fill.color_end
        pieces.append(data[cursor:])
        data = b"".join(pieces)
    if not reverse_colors:
        data = data.replace(_WHITE_RGB, b"1 1 1").replace(_WHITE_GRAY, b"1")
        return data, facts

    def _handle(match: re.Match[bytes]) -> bytes:
        operator = match.group("op")
        if operator in (b"g", b"G"):
            value = float(match.group("v"))
            if value > 1.0:  # inserted white background placeholder
                return match.group(0)
            adapted = adapt_gray(value * 255.0)
            facts["color_operators"] += 1
            return f"{adapted / 255.0:.8f} {operator.decode()}".encode()
        values = [float(match.group("v"))]
        values.extend(float(part) for part in match.group("rgb").split())
        if max(values) > 1.0:  # inserted white background placeholder
            return match.group(0)
        out = adapt_rgb(*(value * 255.0 for value in values))
        facts["color_operators"] += 1
        return (" ".join(f"{v / 255.0:.8f}" for v in out) + " " + operator.decode()).encode()

    data = _substitute_outside(data, _COLOR_OP, _handle, protected)
    data = data.replace(_WHITE_RGB, b"1 1 1").replace(_WHITE_GRAY, b"1")
    return data, facts


def _png_wrap(raw: bytes, width: int, height: int, colors: int, bits: int) -> bytes:
    """Wrap a PNG-filtered Flate stream so Pillow can decode it.

    PDF ``FlateDecode`` with a predictor of 10 or more uses exactly the PNG
    row filters, so the compressed stream can be re-used as a PNG ``IDAT``
    payload instead of re-implementing the predictor in Python.
    """
    import struct
    import zlib

    color_type = {1: 0, 3: 2, 4: 6}.get(colors, 2)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, bits, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def _decode_image(obj: pikepdf.Object) -> tuple[Image.Image, str]:
    """Decode an image XObject to RGB and return ``(image, source_filter)``."""
    raw = obj.read_raw_bytes()
    filt = str(obj.get("/Filter"))
    if "DCTDecode" in filt or "JPXDecode" in filt:
        return Image.open(io.BytesIO(raw)).convert("RGB"), "jpeg"
    if "FlateDecode" in filt or "LZWDecode" in filt or filt == "None":
        width, height = int(obj["/Width"]), int(obj["/Height"])
        color_space = str(obj.get("/ColorSpace"))
        if color_space not in {"/DeviceGray", "/DeviceRGB"}:
            raise ValueError(f"unsupported image colour space {color_space}")
        channels = 1 if color_space == "/DeviceGray" else 3
        parameters = obj.get("/DecodeParms")
        predictor = 1
        colors = channels
        bits = 8
        if parameters is not None:
            predictor = int(parameters.get("/Predictor", 1) or 1)
            colors = int(parameters.get("/Colors", channels) or channels)
            bits = int(parameters.get("/BitsPerComponent", 8) or 8)
        if predictor >= 10:
            png = _png_wrap(raw, width, height, colors, bits)
            return Image.open(io.BytesIO(png)).convert("RGB"), "flate"
        try:
            decoded = zlib.decompress(raw)
        except zlib.error as exc:
            raise ValueError(f"image stream could not be inflated: {exc}") from exc
        array = np.frombuffer(decoded, dtype=np.uint8)
        needed = width * height * channels
        if array.size < needed:
            raise ValueError("image stream is shorter than its declared dimensions")
        array = array[:needed].reshape(height, width, channels)
        if channels == 1:
            array = np.repeat(array, 3, axis=2)
        return Image.fromarray(array, "RGB"), "flate"
    raise ValueError(f"unsupported image filter {filt}")


def _normalize_image(obj: pikepdf.Object) -> tuple[bool, float]:
    """Reverse a genuinely dark image XObject in place; return (changed, mean)."""
    image, encoding = _decode_image(obj)
    values = np.asarray(image).astype(np.int16)
    mean = float(values.mean())
    if mean >= 30.0:
        return False, mean
    low = values.min(axis=2)
    high = values.max(axis=2)
    offset = 255 - low - high
    reversed_values = np.clip(values + offset[..., None], 0, 255).astype(np.uint8)
    out = Image.fromarray(reversed_values, "RGB")
    width, height = int(obj["/Width"]), int(obj["/Height"])
    if encoding == "jpeg":
        buffer = io.BytesIO()
        out.save(buffer, "JPEG", quality=92, subsampling=0)
        obj.write(buffer.getvalue(), filter=pikepdf.Name("/DCTDecode"))
        obj["/Filter"] = pikepdf.Name("/DCTDecode")
    else:
        obj.write(zlib.compress(out.tobytes(), 6), filter=pikepdf.Name("/FlateDecode"))
        obj["/Filter"] = pikepdf.Name("/FlateDecode")
    obj["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
    obj["/BitsPerComponent"] = 8
    obj["/Width"] = width
    obj["/Height"] = height
    for key in ("/Decode", "/DecodeParms"):
        if key in obj:
            del obj[key]
    return True, mean


def inspect_page(page: pikepdf.Page) -> dict:
    """Return structural facts for one page without rendering it."""
    contents = page.obj.get("/Contents")
    if contents is None:
        data = b""
    elif isinstance(contents, pikepdf.Array):
        data = b"\n".join(stream.read_bytes() for stream in contents)
    else:
        data = contents.read_bytes()
    box = page.mediabox
    size = (float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))
    backgrounds = 1 if has_dark_background(data, size) else 0
    images = 0
    dark_images: list[float] = []
    try:
        resources = page.Resources.get("/XObject", {})
    except Exception:
        resources = {}
    for obj in resources.values():
        try:
            if str(obj.get("/Subtype")) != "/Image":
                continue
            images += 1
            image, _ = _decode_image(obj)
            mean = float(np.asarray(image).mean())
            if mean < 30.0:
                dark_images.append(mean)
        except Exception:
            continue
    return {
        "background_fills": backgrounds,
        "images": images,
        "dark_images": dark_images,
        "content_bytes": len(data),
    }


def _page_content_streams(page: pikepdf.Page) -> list[pikepdf.Object]:
    contents = page.obj.get("/Contents")
    if contents is None:
        return []
    return list(contents) if isinstance(contents, pikepdf.Array) else [contents]


def normalize_pdf(
    source: Path,
    output: Path,
    *,
    page_indices: Iterable[int] | None = None,
    dark_pages: Iterable[int] | None = None,
    min_mean: float = 30.0,
) -> PdfReport:
    """Normalize dark pages of ``source`` into a new PDF at ``output``.

    Pages are selected structurally: a dark full-page fill, or a dark image on
    a page that the audit reported as rendering black.  ``page_indices`` can
    narrow the repair to audited pages and ``dark_pages`` states which of them
    actually rendered black, so a page with a light paper background and a
    dark figure is left alone.  The output is re-rendered with pypdfium2 and
    black pages are recorded, but the caller remains responsible for rejecting
    a failed verification.
    """
    source, output = Path(source), Path(output)
    selected = set(page_indices) if page_indices is not None else None
    rendered_dark = set(dark_pages) if dark_pages is not None else None
    report = PdfReport(str(source), str(output), 0)
    with pikepdf.open(source) as pdf:
        report.pages = len(pdf.pages)
        for index, page in enumerate(pdf.pages):
            if selected is not None and index not in selected:
                continue
            page_report = PageReport(index=index)
            contents = _page_content_streams(page)
            box = page.mediabox
            page_size = (float(box[2]) - float(box[0]), float(box[3]) - float(box[1]))
            try:
                resources = page.Resources.get("/XObject", {})
            except Exception:
                resources = {}
            dark_images: list[tuple[pikepdf.Object, float]] = []
            for obj in resources.values():
                try:
                    if str(obj.get("/Subtype")) != "/Image":
                        continue
                    image, _ = _decode_image(obj)
                    mean = float(np.asarray(image).mean())
                    page_report.images += 1
                    if mean < min_mean:
                        dark_images.append((obj, mean))
                except Exception as exc:
                    page_report.detail.append(f"image inspection skipped: {exc}")
            page_has_dark_background = any(
                has_dark_background(stream.read_bytes(), page_size) for stream in contents
            )
            page_is_black = (
                rendered_dark is None
                or index in rendered_dark
                or page_has_dark_background
            )
            if page_has_dark_background or (page_is_black and dark_images):
                for stream in contents:
                    data = stream.read_bytes()
                    normalized, facts = normalize_content_stream(data, page_size=page_size)
                    page_report.background_fills += facts["background_fills"]
                    page_report.color_operators += facts["color_operators"]
                    if normalized != data:
                        stream.write(normalized)
                        page_report.repaired = True
                if page_is_black:
                    for obj, mean in dark_images:
                        try:
                            changed, _ = _normalize_image(obj)
                            if changed:
                                page_report.repaired = True
                                page_report.detail.append(f"dark image mean={mean:.2f} reversed")
                        except Exception as exc:
                            page_report.detail.append(f"image repair skipped: {exc}")
            if page_report.repaired:
                report.repaired_pages.append(index)
            report.page_reports.append(page_report)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not report.repaired_pages:
            shutil.copy2(source, output)
            return report
        pdf.save(output, compress_streams=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return report


def render_page_stats(path: Path, *, scale: float = 24 / 72) -> list[dict]:
    """Render every page as grayscale and return luminance/text statistics."""
    stats: list[dict] = []
    document = pdfium.PdfDocument(Path(path))
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=scale, grayscale=True)
                array = bitmap.to_numpy().reshape(bitmap.height, bitmap.width)
                histogram = np.bincount(array.ravel(), minlength=256)
                try:
                    text_page = page.get_textpage()
                    text = text_page.get_text_bounded()
                    text_page.close()
                except Exception:
                    text = ""
                stats.append({
                    "index": index,
                    "mean": round(float(array.mean()), 4),
                    "std": round(float(array.std()), 4),
                    "min": int(array.min()),
                    "max": int(array.max()),
                    "dark_fraction": round(float(histogram[:16].sum() / array.size), 5),
                    "white_fraction": round(float(histogram[240:].sum() / array.size), 5),
                    "text_chars": len(text),
                })
                bitmap.close()
            finally:
                page.close()
    finally:
        document.close()
    return stats
