#!/usr/bin/env python3
"""Compatibility import for PyMuPDF.

Some environments accidentally install the unrelated `fitz` package, which
shadows PyMuPDF and crashes on import. This helper normalizes the import path
and surfaces a clear fix when PyMuPDF is missing.
"""

from __future__ import annotations

from pathlib import Path

from python_runtime_bootstrap import bootstrap_python_paths

bootstrap_python_paths(Path(__file__).resolve())

PYMUPDF_INSTALL_HINT = (
    "PyMuPDF is required for PDF processing, but this Python environment is "
    "loading the unrelated `fitz` package or does not have PyMuPDF installed.\n"
    "Fix it with:\n"
    "  python3 -m pip uninstall -y fitz frontend\n"
    "  python3 -m pip install --user --upgrade PyMuPDF"
)


def _looks_like_pymupdf(module: object) -> bool:
    return all(hasattr(module, attr) for attr in ("open", "Matrix", "Rect", "Point"))


def import_fitz():
    last_error: Exception | None = None

    try:
        import fitz as candidate  # type: ignore

        if _looks_like_pymupdf(candidate):
            return candidate
    except Exception as exc:  # pragma: no cover - depends on local environment
        last_error = exc

    try:
        import pymupdf as candidate  # type: ignore

        if _looks_like_pymupdf(candidate):
            return candidate
    except Exception as exc:  # pragma: no cover - depends on local environment
        if last_error is None:
            last_error = exc

    raise RuntimeError(PYMUPDF_INSTALL_HINT) from last_error


fitz = import_fitz()