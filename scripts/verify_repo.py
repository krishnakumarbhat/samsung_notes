#!/usr/bin/env python3
"""Repository hygiene gate for the Samsung Notes workspace.

Runs the checks that must hold before every push:

* no tracked file approaches GitHub's 100 MB hard limit (99,000,000 bytes);
* no tracked ``.env`` file, embedded key or private key;
* every tracked JSON file parses;
* every tracked Python file compiles and every shell script parses;
* relative Markdown links resolve;
* operational scripts remain executable;
* required replication files exist.

The checker is standard-library only and works on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 99_000_000
PRIVATE_PREFIXES = (
    "01_vm_windows_sync/vm/",
    "02_extracted_notes_pdf/notes/",
    "02_extracted_notes_pdf/ai_agend_history/",
    "04_learning_recall_app/my_notes_graph/data/",
)
PRIVATE_SUFFIXES = (
    ".pdf",
    ".sdocx",
    ".qcow2",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".db",
    ".page",
    ".spi",
    ".note",
)
SECRET_PATTERNS = (
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "password-assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*[\"']?"
            r"(?!your[_-]|example|placeholder|redacted|changeme|demo[_-]|"
            r"test-not-a-real-key|settings(?:\.|\b)|self(?:\.|\b)|"
            r"<|\$|\.\.\.|none\b|null\b)"
            r"[A-Za-z0-9!@#$%^&*_\-]{8,}"
        ),
    ),
)
REQUIRED_PATHS = (
    "README.md",
    "REPLICATION.md",
    "Makefile",
    ".gitattributes",
    "docs/ARCHITECTURE.md",
    "docs/DATA_AND_ASSETS.md",
    "docs/RESEARCH.md",
    "docs/TROUBLESHOOTING.md",
    "docs/EVIDENCE_INDEX.md",
    "manifests/README.md",
    "scripts/bootstrap.sh",
    "scripts/doctor.py",
    "scripts/run_tests.py",
    "scripts/verify_repo.py",
    "scripts/assets_manifest.py",
    "scripts/pack_local_assets.sh",
    "scripts/restore_local_assets.sh",
    "scripts/fetch_models.py",
    "requirements/app.txt",
    "requirements/export.txt",
    "requirements/ocr.txt",
    "02_extracted_notes_pdf/README.md",
    "02_extracted_notes_pdf/.env.example",
    "04_learning_recall_app/my_notes_graph/README.md",
    "05_environments_and_venvs/README.md",
    "tests/test_repo_hygiene.py",
    ".github/workflows/ci.yml",
)


@dataclass
class Finding:
    """One hygiene result."""

    check: str
    status: str
    detail: str


def _git(root: Path, *args: str) -> str:
    """Run a git command and return stripped output, or an empty string."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def tracked_files(root: Path) -> list[Path]:
    """Return tracked files, falling back to a filtered walk outside Git."""
    output = _git(root, "ls-files", "-z")
    if output:
        return [root / name for name in output.split("\0") if name]
    skip = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "site-packages"}
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in skip)
        files.extend(Path(directory) / name for name in sorted(filenames))
    return files


def _is_text(path: Path) -> bool:
    """Detect text by looking for NUL bytes in the first block."""
    try:
        return b"\0" not in path.read_bytes()[:8192]
    except OSError:
        return False


def check_sizes(root: Path, files: list[Path]) -> list[Finding]:
    """Reject tracked files at or above the 99 MB budget."""
    findings: list[Finding] = []
    oversized: list[tuple[int, str]] = []
    large: list[tuple[int, str]] = []
    for path in files:
        try:
            size = path.lstat().st_size
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        if size >= MAX_BYTES:
            oversized.append((size, relative))
        elif size >= 50_000_000:
            large.append((size, relative))
    if oversized:
        findings.append(
            Finding(
                "file-size",
                "fail",
                "tracked files >= 99 MB: " + ", ".join(f"{name} ({size / 1e6:.1f} MB)" for size, name in oversized),
            )
        )
    else:
        findings.append(Finding("file-size", "ok", f"largest tracked file is below 99 MB"))
    if large:
        findings.append(
            Finding(
                "large-file-warning",
                "warn",
                "tracked files >= 50 MB: " + ", ".join(f"{name} ({size / 1e6:.1f} MB)" for size, name in large),
            )
        )
    return findings


def check_private_payloads(root: Path, files: list[Path]) -> list[Finding]:
    """Reject note data, VM images, user databases and machine environments."""
    unsafe = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative.startswith(PRIVATE_PREFIXES) or relative.lower().endswith(PRIVATE_SUFFIXES):
            unsafe.append(relative)
    if unsafe:
        return [Finding("private-payloads", "fail", "local-only files tracked: " + ", ".join(unsafe[:10]))]
    return [Finding("private-payloads", "ok", "no note payloads, VM images, user databases or local environments tracked")]


def check_env_and_secrets(root: Path, files: list[Path]) -> list[Finding]:
    """Ensure no environment file or credential is tracked."""
    findings: list[Finding] = []
    tracked_env = [
        path.relative_to(root).as_posix()
        for path in files
        if path.name == ".env" or path.name.endswith(".env.local")
    ]
    if tracked_env:
        findings.append(Finding("tracked-env", "fail", "tracked environment files: " + ", ".join(tracked_env)))
    else:
        findings.append(Finding("tracked-env", "ok", "no .env file is tracked"))

    hits: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        if not _is_text(path):
            continue
        relative = path.relative_to(root).as_posix()
        if "/vendor/" in relative or relative.endswith((".min.js", ".min.css")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{relative}:{line} ({label})")
                if len(hits) >= 20:
                    break
            if len(hits) >= 20:
                break
    if hits:
        findings.append(Finding("secret-scan", "fail", "possible secrets: " + "; ".join(hits)))
    else:
        findings.append(Finding("secret-scan", "ok", "no credential patterns in tracked text files"))
    return findings


def check_json(root: Path, files: list[Path]) -> list[Finding]:
    """Parse every tracked JSON document below 20 MB."""
    failures: list[str] = []
    checked = 0
    for path in files:
        if path.suffix.lower() != ".json" or not path.is_file():
            continue
        try:
            if path.lstat().st_size > 20_000_000:
                continue
            json.loads(path.read_text(encoding="utf-8"))
            checked += 1
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"{path.relative_to(root).as_posix()}: {exc}")
    if failures:
        return [Finding("json", "fail", "; ".join(failures[:10]))]
    return [Finding("json", "ok", f"{checked} JSON documents parse")]


def check_python(root: Path, files: list[Path]) -> list[Finding]:
    """Compile tracked Python sources without writing bytecode."""
    failures: list[str] = []
    checked = 0
    for path in files:
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
            checked += 1
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            failures.append(f"{path.relative_to(root).as_posix()}: {exc}")
    if failures:
        return [Finding("python-syntax", "fail", "; ".join(failures[:10]))]
    return [Finding("python-syntax", "ok", f"{checked} Python files compile")]


def check_shell(root: Path, files: list[Path]) -> list[Finding]:
    """Parse tracked shell scripts with ``bash -n`` when bash is available."""
    bash = shutil.which("bash")
    if not bash:
        return [Finding("shell-syntax", "skip", "bash not available")]
    failures: list[str] = []
    checked = 0
    for path in files:
        if (path.suffix not in {".sh"} and not path.name.endswith(".bash")) or not path.is_file():
            continue
        try:
            result = subprocess.run(
                [bash, "-n", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode:
                failures.append(f"{path.relative_to(root).as_posix()}: {result.stderr.strip()[:300]}")
            else:
                checked += 1
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{path.relative_to(root).as_posix()}: {exc}")
    if failures:
        return [Finding("shell-syntax", "fail", "; ".join(failures[:10]))]
    return [Finding("shell-syntax", "ok", f"{checked} shell scripts parse")]


LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def check_markdown_links(root: Path, files: list[Path]) -> list[Finding]:
    """Validate relative links against files that will exist in a clean clone."""
    missing: list[str] = []
    checked = 0
    tracked = {path.resolve() for path in files if path.exists()}
    for path in files:
        if path.suffix.lower() not in {".md", ".markdown"} or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_target in LINK.findall(text):
            target = raw_target.strip().split("#", 1)[0]
            if "{" in target or "}" in target or "..." in target:
                continue  # Template examples, not concrete links.
            if not target or target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                continue
            if target.startswith(("/home/", "/media/", "C:\\", "~")):
                continue
            target = target.replace("%20", " ")
            candidate = (path.parent / target).resolve()
            checked += 1
            tracked_target = candidate in tracked or (
                candidate.is_dir()
                and any(str(item).startswith(str(candidate) + os.sep) for item in tracked)
            )
            if not candidate.exists() or not tracked_target:
                missing.append(f"{path.relative_to(root).as_posix()} -> {raw_target}")
    if missing:
        return [Finding("markdown-links", "fail", "; ".join(missing[:15]))]
    return [Finding("markdown-links", "ok", f"{checked} relative links resolve")]


def check_required(root: Path) -> list[Finding]:
    """Assert the replication entry points exist."""
    missing = [name for name in REQUIRED_PATHS if not (root / name).exists()]
    if missing:
        return [Finding("replication-files", "fail", "missing: " + ", ".join(missing))]
    return [Finding("replication-files", "ok", f"{len(REQUIRED_PATHS)} replication files present")]


def check_executables(root: Path, files: list[Path]) -> list[Finding]:
    """Keep launcher scripts executable on POSIX filesystems."""
    if os.name == "nt":
        return [Finding("executable-bit", "skip", "POSIX mode bits are not meaningful on Windows")]
    expected = [
        path
        for path in files
        if path.is_file() and path.name != "__init__.py" and path.suffix in {".sh", ".py"} and (
            path.parent.name in {"scripts"}
            or path.name in {"run_app.sh", "run_ocr.sh", "rebuild_index_offline.sh", "build-vm.sh"}
        )
    ]
    bad = [
        path.relative_to(root).as_posix()
        for path in expected
        if not (path.stat().st_mode & 0o111)
    ]
    if bad:
        return [Finding("executable-bit", "fail", "not executable: " + ", ".join(bad))]
    return [Finding("executable-bit", "ok", f"{len(expected)} launchers executable")]


def run(root: Path = ROOT) -> list[Finding]:
    """Run every hygiene check in a deterministic order."""
    files = tracked_files(root)
    findings: list[Finding] = [Finding("tracked-files", "ok", f"{len(files)} files tracked")]
    findings += check_required(root)
    findings += check_sizes(root, files)
    findings += check_private_payloads(root, files)
    findings += check_env_and_secrets(root, files)
    findings += check_json(root, files)
    findings += check_python(root, files)
    findings += check_shell(root, files)
    findings += check_markdown_links(root, files)
    findings += check_executables(root, files)
    return findings


def render(findings: list[Finding]) -> str:
    """Format findings for a terminal."""
    symbols = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    width = max(len(item.check) for item in findings)
    lines = [f"Repository hygiene  ({ROOT})", ""]
    for item in findings:
        lines.append(f"  [{symbols.get(item.status, 'INFO')}] {item.check:<{width}}  {item.detail}")
    failures = [item for item in findings if item.status == "fail"]
    lines.append("")
    lines.append(f"{len(findings)} checks, {len(failures)} failure(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    findings = run()
    failed = [item for item in findings if item.status == "fail"]
    warned = [item for item in findings if item.status == "warn"]
    if args.json:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "root": str(ROOT),
                    "summary": {"failed": len(failed), "warnings": len(warned)},
                    "findings": [asdict(item) for item in findings],
                },
                indent=2,
            )
        )
    else:
        print(render(findings))
    if failed:
        return 1
    if args.strict and warned:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
