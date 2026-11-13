#!/usr/bin/env python3
"""Record the interpreter, platform and package versions used by the tests.

The output is a small JSON file so a future machine can compare its own
environment against the version that passed the suite.  It is not a lock file:
the exact pins live in ``requirements/*.txt``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "manifests/environment.json"
PACKAGES = (
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
    "fsrs",
    "pypdf",
    "httpx",
    "pytest",
    "pytest-asyncio",
    "playwright",
    "sentence-transformers",
    "transformers",
    "torch",
    "numpy",
    "PyMuPDF",
    "pikepdf",
    "paddlepaddle",
    "paddleocr",
    "opencv-python-headless",
    "chromadb",
)


def _version(name: str) -> str:
    """Return an installed version or ``missing`` without failing."""
    try:
        return version(name)
    except PackageNotFoundError:
        return "missing"


def _git(*args: str) -> str:
    """Read a git value when the repository is available."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    """Write the environment report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    external = {
        "python_libs": ROOT.parent / "python_libs/site-packages",
        "python_libs_gpu": ROOT.parent / "python_libs_gpu/site-packages",
        "ocr_site_packages": ROOT / "05_environments_and_venvs/python_ocr_env/site-packages",
        "shared_venv": Path.home() / ".cache/sam_win/venv",
    }
    payload = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": Path(sys.executable).name,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "cwd": ".",
        "git": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
        },
        "external_roots": {name: {"present": path.is_dir()} for name, path in external.items()},
        "packages": [{"name": name, "version": _version(name)} for name in PACKAGES],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
