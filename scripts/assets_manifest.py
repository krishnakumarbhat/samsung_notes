#!/usr/bin/env python3
"""Inventory and verify the local-only assets that are too large for Git.

GitHub rejects individual files over 100 MB and a multi-gigabyte repository is
not a backup strategy.  This tool records exactly which local files make the
workspace reproducible, with sizes, symlink targets and optional SHA-256
digests.  The resulting JSON is small enough to commit; the payload itself is
transferred with ``scripts/pack_local_assets.sh`` or any file-sync tool.

Usage
    python scripts/assets_manifest.py generate [--hash] [--group notes]
    python scripts/assets_manifest.py verify   [--hash] [--strict]
    python scripts/assets_manifest.py list
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "manifests/local_assets.private.json"
SCHEMA = 1


@dataclass
class Group:
    """A logical asset set with include/exclude patterns relative to its root."""

    name: str
    root: str
    description: str
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = field(default_factory=tuple)
    hash_by_default: bool = True


GROUPS: tuple[Group, ...] = (
    Group(
        "vm",
        "01_vm_windows_sync/vm",
        "Windows VM disk, OVMF vars and QEMU logs (privacy-sensitive; never commit)",
        include=("*.qcow2", "*.fd", ".installed", "qemu.log"),
        exclude=("*.pid", "*.sock"),
        hash_by_default=False,
    ),
    Group(
        "notes",
        "02_extracted_notes_pdf/notes",
        "Exported original and compressed Samsung Notes PDFs",
        exclude=("*.tmp",),
    ),
    Group(
        "history",
        "02_extracted_notes_pdf/ai_agend_history",
        "Export state, manifests, preserved originals and research evidence",
        exclude=("page_cache/**", "ocr_visual_cache/**", "*.lock", "*.log", "*.pid"),
    ),
    Group(
        "models",
        "05_environments_and_venvs/models",
        "Pinned BGE-small embedding model (weights are re-downloadable)",
    ),
    Group(
        "app_data",
        "04_learning_recall_app/my_notes_graph/data",
        "Local retrieval index, FSRS history and study vault",
        exclude=("chroma/**", "*.pid", "*.lock", "*.log"),
    ),
    Group(
        "notes_mirror",
        "04_learning_recall_app/notes_mirror",
        "Legacy mirrored export manifest used by the recall app",
    ),
    Group(
        "ocr_cache",
        "03_rtx_compression_ocr/ocr_visual_cache",
        "Optional visual OCR cache (regenerable; excluded by default from bundles)",
        exclude=("**/*.tmp",),
        hash_by_default=False,
    ),
)


def _relative(path: Path) -> str:
    """Return a stable POSIX path relative to the repository root."""
    return path.relative_to(ROOT).as_posix()


def _matches(relative: str, patterns: Iterable[str]) -> bool:
    """Match a POSIX relative path against shell-style patterns."""
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _iter_group(group: Group) -> Iterable[Path]:
    """Yield files and symlinks belonging to one asset group in sorted order."""
    base = ROOT / group.root
    if not base.exists():
        return
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in {"__pycache__", ".pytest_cache"}
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            relative = _relative(path)
            suffix = relative[len(group.root) + 1 :] if relative.startswith(group.root + "/") else relative
            if not _matches(suffix, group.include):
                continue
            if _matches(suffix, group.exclude):
                continue
            yield path


def _sha256(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    """Hash a file with bounded memory; large VM images stream safely."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(path: Path, do_hash: bool) -> dict:
    """Describe one asset without following symlinks."""
    stat = path.lstat()
    relative = _relative(path)
    entry = {
        "path": relative,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mode": oct(stat.st_mode & 0o777),
    }
    if path.is_symlink():
        entry["type"] = "symlink"
        entry["target"] = os.readlink(path)
        entry["bytes"] = 0
        return entry
    entry["type"] = "file"
    if do_hash:
        entry["sha256"] = _sha256(path)
    return entry


def generate(
    selected: set[str] | None,
    do_hash: bool,
    workers: int,
    output: Path,
) -> dict:
    """Build the manifest and write it atomically unless output is ``-``."""
    groups: list[dict] = []
    all_entries: list[dict] = []
    jobs: list[tuple[Group, Path]] = []
    for group in GROUPS:
        if selected and group.name not in selected:
            continue
        files = list(_iter_group(group))
        jobs.extend((group, path) for path in files)
        groups.append(
            {
                "name": group.name,
                "root": group.root,
                "description": group.description,
                "files": len(files),
                "bytes": sum(path.lstat().st_size for path in files),
            }
        )

    hashed_groups = {
        group.name for group in GROUPS if do_hash and (not selected or group.name in selected)
    }
    by_path: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        for group, path in jobs:
            should_hash = group.name in hashed_groups and group.hash_by_default
            futures[pool.submit(_entry, path, should_hash)] = path
        for future in futures:
            entry = future.result()
            by_path[entry["path"]] = entry
            all_entries.append(entry)

    all_entries.sort(key=lambda item: item["path"])
    manifest = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(ROOT),
        "policy": {
            "github_file_limit_bytes": 100_000_000,
            "committed_limit_bytes": 99_000_000,
            "large_files": "recorded here and transferred outside Git",
        },
        "totals": {
            "files": len(all_entries),
            "bytes": sum(entry["bytes"] for entry in all_entries),
            "hashed_files": sum("sha256" in entry for entry in all_entries),
        },
        "groups": groups,
        "files": all_entries,
    }
    if str(output) != "-":
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
    return manifest


def public_summary(manifest: dict) -> dict:
    """Return a privacy-safe inventory without local payload names or paths."""
    safe_groups = [
        {key: group[key] for key in ("name", "files", "bytes")}
        for group in manifest["groups"]
    ]
    tool = ROOT / "tools/sdocx-cli"
    tool_entry = _entry(tool, True) if tool.is_file() else None
    files = [tool_entry] if tool_entry else []
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": "Aggregate counts only; local paths and note names are not published.",
        "groups": safe_groups,
        "totals": {
            "files": sum(group["files"] for group in safe_groups),
            "bytes": sum(group["bytes"] for group in safe_groups),
        },
        "reproducible_tool": files,
    }


def _load(path: Path) -> dict:
    """Read a manifest and validate its basic schema."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise SystemExit(f"unsupported manifest schema in {path}")
    if not isinstance(data.get("files"), list):
        raise SystemExit(f"manifest has no files list: {path}")
    return data


def verify(path: Path, do_hash: bool, strict: bool) -> tuple[int, list[str]]:
    """Check sizes, symlink targets and optionally hashes from a manifest."""
    data = _load(path)
    problems: list[str] = []
    checked = 0
    for entry in data["files"]:
        relative = entry["path"]
        candidate = ROOT / relative
        checked += 1
        if not candidate.exists() and not candidate.is_symlink():
            problems.append(f"missing: {relative}")
            continue
        if entry.get("type") == "symlink":
            if not candidate.is_symlink():
                problems.append(f"expected symlink: {relative}")
            elif os.readlink(candidate) != entry.get("target"):
                problems.append(f"symlink target changed: {relative}")
            continue
        size = candidate.lstat().st_size
        if size != entry["bytes"]:
            problems.append(f"size mismatch: {relative} ({size} != {entry['bytes']})")
        if do_hash and entry.get("sha256"):
            actual = _sha256(candidate)
            if actual != entry["sha256"]:
                problems.append(f"sha256 mismatch: {relative}")
    if strict:
        known = {entry["path"] for entry in data["files"]}
        for group in data.get("groups", []):
            base = ROOT / group["root"]
            if not base.exists():
                continue
            for directory, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [name for name in dirnames if name not in {"__pycache__"}]
                for filename in filenames:
                    relative = _relative(Path(directory) / filename)
                    if relative not in known:
                        problems.append(f"unexpected: {relative}")
    return checked, problems


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "verify", "list", "totals"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--group", action="append", choices=[group.name for group in GROUPS])
    parser.add_argument("--hash", action="store_true", help="compute SHA-256 for every selected file")
    parser.add_argument("--no-hash", action="store_true", help="sizes and symlinks only")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 2))
    parser.add_argument("--strict", action="store_true", help="fail on unexpected files in a group")
    parser.add_argument("--public-summary", action="store_true", help="write aggregate metadata only")
    args = parser.parse_args(argv)

    if args.command == "list":
        for group in GROUPS:
            print(f"{group.name:14} {group.root:55} {group.description}")
        return 0

    if args.command == "totals":
        data = _load(args.output)
        for group in data.get("groups", []):
            print(f"{group['name']:14} {group['files']:6} files  {group['bytes'] / 1e9:9.3f} GB")
        print(
            f"{'TOTAL':14} {data['totals']['files']:6} files  "
            f"{data['totals']['bytes'] / 1e9:9.3f} GB"
        )
        return 0

    if args.command == "generate":
        manifest = generate(
            set(args.group) if args.group else None,
            do_hash=args.hash and not args.no_hash,
            workers=args.workers,
            output=args.output,
        )
        if args.public_summary:
            public = public_summary(manifest)
            public_output = ROOT / "manifests/local_assets.json"
            public_output.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
            print(f"wrote privacy-safe summary {public_output}")
        if str(args.output) != "-":
            print(
                f"wrote {args.output} ({manifest['totals']['files']} files, "
                f"{manifest['totals']['bytes'] / 1e9:.3f} GB, "
                f"{manifest['totals']['hashed_files']} hashed)"
            )
        return 0

    checked, problems = verify(args.output, do_hash=args.hash, strict=args.strict)
    if problems:
        print(f"FAIL: {len(problems)} problem(s) across {checked} assets", file=sys.stderr)
        for problem in problems[:100]:
            print("  " + problem, file=sys.stderr)
        if len(problems) > 100:
            print(f"  ... and {len(problems) - 100} more", file=sys.stderr)
        return 1
    print(f"OK: {checked} assets verified against {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
