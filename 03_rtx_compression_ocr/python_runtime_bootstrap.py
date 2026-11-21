#!/usr/bin/env python3
"""Bootstrap the shared CPU/GPU Python library roots used by OCR tools."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []

    for path in paths:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)

    return unique


def _get_script_project_root(script_path: Path) -> Path | None:
    """Find sam_win by its numbered workspace marker instead of fixed depth."""
    for parent in script_path.resolve().parents:
        if (parent / "01_vm_windows_sync").is_dir() and (
            parent / "05_environments_and_venvs"
        ).is_dir():
            return parent
    return None


def get_candidate_library_roots(script_path: Path | None = None) -> list[Path]:
    """Return only the consolidated GPU and CPU site-package roots."""
    script_path = script_path or Path(__file__).resolve()
    preferred_variant = os.environ.get("SAM_PDF_PADDLE_VARIANT", "auto").strip().lower()
    project_root = _get_script_project_root(script_path)
    if project_root is None:
        return []
    shared_root = project_root.parent
    gpu_root = shared_root / "python_libs_gpu"
    cpu_root = shared_root / "python_libs"
    candidates = [cpu_root] if preferred_variant == "cpu" else [gpu_root, cpu_root]
    return [path for path in _dedupe_paths(candidates) if path.exists()]


def _deprioritize_user_site_packages() -> None:
    """Keep shared user packages available after the coherent OCR/GPU roots."""
    user_site = site.getusersitepackages()
    if not user_site:
        return

    sys.path[:] = [entry for entry in sys.path if entry != user_site]
    if Path(user_site).is_dir():
        sys.path.append(user_site)


def bootstrap_python_paths(script_path: Path | None = None) -> list[str]:
    inserted_set: set[str] = set()

    _deprioritize_user_site_packages()

    for root in reversed(get_candidate_library_roots(script_path)):
        site_packages = root / "site-packages"
        candidate_paths = [site_packages] if site_packages.is_dir() else [root]
        for candidate in reversed(candidate_paths):
            if not candidate.is_dir():
                continue
            candidate_str = str(candidate)
            if candidate_str in sys.path:
                continue
            sys.path.insert(0, candidate_str)
            inserted_set.add(candidate_str)

    return [path for path in sys.path if path in inserted_set]
