#!/usr/bin/env python3
"""Run every test suite with the correct interpreter and PYTHONPATH.

The workspace intentionally keeps package trees outside the repository
(``../python_libs`` and ``../python_libs_gpu``). Running ``pytest`` by hand
picks the wrong ``pytest`` or a broken plugin, so this launcher builds a
coherent environment first.

Examples
    python scripts/run_tests.py
    python scripts/run_tests.py --suite app --suite ocr
    python scripts/run_tests.py --fast --json-out /tmp/tests.json
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Suite:
    """One pytest invocation rooted at a subsystem directory."""

    name: str
    directory: Path
    args: tuple[str, ...]
    description: str
    platforms: tuple[str, ...] = ("linux", "darwin", "win32")
    fast_args: tuple[str, ...] = ()


SUITES: tuple[Suite, ...] = (
    Suite(
        "app",
        ROOT / "04_learning_recall_app/my_notes_graph",
        ("tests", "-q"),
        "FastAPI recall dashboard, FSRS, quiz bank and local index",
        fast_args=("tests", "-q", "-k", "not local_index and not e2e"),
    ),
    Suite(
        "pipeline",
        ROOT / "02_extracted_notes_pdf",
        ("-q",),
        "Incremental exporter, page delta, PDF repair and colour normalisation",
        platforms=("linux", "darwin"),
    ),
    Suite(
        "ocr",
        ROOT / "03_rtx_compression_ocr",
        ("-q",),
        "PaddleOCR bridge, invisible text and lossless compression",
        platforms=("linux", "darwin"),
    ),
    Suite(
        "guestsetup",
        ROOT / "01_vm_windows_sync/guestsetup",
        ("-q",),
        "Windows sync quiescence helpers",
        platforms=("linux", "darwin"),
    ),
)


def _venv_python(root: Path) -> Path | None:
    """Find the first usable interpreter from the supported locations."""
    candidates = [
        os.environ.get("SAM_PYTHON"),
        os.environ.get("OPENCODE_PYTHON"),
        str(Path(os.environ.get("SAM_VENV", "")) / "bin/python3") if os.environ.get("SAM_VENV") else None,
        str(root / ".venv/bin/python3"),
        str(root / ".venv/Scripts/python.exe"),
        sys.executable,
        shutil.which("python3"),
        shutil.which("python"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _site_roots(root: Path) -> list[Path]:
    """Return external package roots in import-priority order."""
    parents = root.parent
    candidates = [
        root / "04_learning_recall_app/my_notes_graph",
        root / "02_extracted_notes_pdf/script",
        root / "03_rtx_compression_ocr",
        parents / "python_libs_gpu/site-packages",
        parents / "python_libs/site-packages",
    ]
    return [path for path in candidates if path.is_dir()]


def build_env(root: Path) -> dict[str, str]:
    """Build a deterministic test environment without mutating the caller's."""
    env = dict(os.environ)
    notes_root = root.parent / "sam_notes"
    notes_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"_run_tmp_{os.getpid()}.", dir=notes_root))
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    pytest_root = Path("/tmp/opencode")
    pytest_root.mkdir(parents=True, exist_ok=True)
    pytest_scratch = Path(tempfile.mkdtemp(prefix=f"sam_win_pytest_{os.getpid()}.", dir=pytest_root))
    atexit.register(shutil.rmtree, pytest_scratch, ignore_errors=True)
    env["SAM_NOTES_ROOT"] = str(notes_root)
    env["SN_EXPORT_WORK"] = str(pytest_scratch)
    # pytest tmp_path exercises private-mode token files; keep test scratch on
    # the native host filesystem. Production runtime staging remains in sam_notes.
    env["TMPDIR"] = str(pytest_scratch)
    env["PADDLE_PDX_CACHE_HOME"] = str(scratch / "paddlex")
    env["PADDLE_HOME"] = str(scratch / "paddle")
    env["MPLCONFIGDIR"] = str(scratch / "matplotlib")
    env["CUDA_CACHE_PATH"] = str(scratch / "cuda-cache")
    for path in (env["PADDLE_PDX_CACHE_HOME"], env["PADDLE_HOME"],
                 env["MPLCONFIGDIR"], env["CUDA_CACHE_PATH"]):
        Path(path).mkdir(parents=True, exist_ok=True)
    site_roots = _site_roots(root)
    parts = [str(path) for path in site_roots]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONNOUSERSITE"] = "1"
    native_paths = []
    for base in site_roots:
        for pattern in ("nvidia/*/lib", "*.libs"):
            native_paths.extend(str(path) for path in base.glob(pattern) if path.is_dir())
    if native_paths:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            native_paths + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
        )
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env.setdefault("OMP_NUM_THREADS", "2")
    env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    env.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")
    return env


def _pytest_available(python: Path, env: dict[str, str]) -> tuple[bool, str]:
    """Check that the selected interpreter can import pytest."""
    result = subprocess.run(
        [str(python), "-c", "import pytest; print(pytest.__version__)"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def selected_suites(names: list[str]) -> list[Suite]:
    """Validate and return the requested suite objects."""
    if not names:
        return [suite for suite in SUITES if sys.platform in suite.platforms]
    known = {suite.name for suite in SUITES}
    unknown = sorted(set(names) - known)
    if unknown:
        raise SystemExit(f"unknown suite(s): {', '.join(unknown)}; choose from {', '.join(sorted(known))}")
    return [suite for suite in SUITES if suite.name in names]


def main(argv: list[str] | None = None) -> int:
    """Run the selected suites and print a compact summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", default=[], help="suite to run (repeatable)")
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    parser.add_argument("--fast", action="store_true", help="skip the slow local-index/e2e app tests")
    parser.add_argument("--json-out", type=Path, help="write machine-readable results")
    parser.add_argument("--verbose", action="store_true", help="stream pytest output")
    args = parser.parse_args(argv)

    if args.list:
        for suite in SUITES:
            print(f"{suite.name:12} {suite.directory.relative_to(ROOT)}  {suite.description}")
        return 0

    python = _venv_python(ROOT)
    if python is None:
        print("No Python interpreter found. Run `make bootstrap` first.", file=sys.stderr)
        return 2
    env = build_env(ROOT)
    available, version = _pytest_available(python, env)
    if not available:
        print(
            f"pytest is not importable with {python} ({version}).\n"
            "Run `make bootstrap` or `python -m pip install -r requirements/dev.txt`.",
            file=sys.stderr,
        )
        return 2

    suites = selected_suites(args.suite)
    results = []
    started_all = time.time()
    for suite in suites:
        if sys.platform not in suite.platforms:
            results.append(
                {
                    "suite": suite.name,
                    "status": "skipped",
                    "returncode": None,
                    "seconds": 0,
                    "detail": f"not supported on {sys.platform}; use WSL2/Linux",
                }
            )
            continue
        if not suite.directory.is_dir():
            results.append(
                {
                    "suite": suite.name,
                    "status": "skipped",
                    "returncode": None,
                    "seconds": 0,
                    "detail": "directory absent",
                }
            )
            continue
        suite_args = list(suite.fast_args if args.fast and suite.fast_args else suite.args)
        command = [str(python), "-m", "pytest", *suite_args, "-p", "no:cacheprovider"]
        print(f"\n=== {suite.name}: {' '.join(command)}  (cwd={suite.directory.relative_to(ROOT)}) ===")
        started = time.time()
        if args.verbose:
            completed = subprocess.run(command, cwd=suite.directory, env=env, check=False)
            output = ""
        else:
            completed = subprocess.run(
                command,
                cwd=suite.directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            output = (completed.stdout + completed.stderr).strip()
            if output:
                print(output[-4000:])
        elapsed = round(time.time() - started, 2)
        results.append(
            {
                "suite": suite.name,
                "status": "passed" if completed.returncode == 0 else "failed",
                "returncode": completed.returncode,
                "seconds": elapsed,
                "command": command,
                "detail": output[-2000:] if output else "",
            }
        )

    total = round(time.time() - started_all, 2)
    failed = [item for item in results if item["status"] == "failed"]
    passed = [item for item in results if item["status"] == "passed"]
    skipped = [item for item in results if item["status"] == "skipped"]
    summary = {
        "schema": 1,
        "python": str(python),
        "pytest": version,
        "seconds": total,
        "suites": results,
        "totals": {"passed": len(passed), "failed": len(failed), "skipped": len(skipped)},
    }
    print(
        f"\nSUMMARY: {len(passed)} suite(s) passed, {len(failed)} failed, "
        f"{len(skipped)} skipped in {total:.1f}s using {python} (pytest {version})"
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
