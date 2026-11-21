#!/usr/bin/env python3
"""Check whether this machine can run the Samsung Notes workspace.

The doctor is deliberately dependency-free: it uses only the standard library
so it can run before the Python environment is bootstrapped.  It checks the
repository layout, Python version, optional commands, credentials layout,
local-only assets and the two hard disk guardrails (55 GB workspace cap and
15 GiB free-space floor).

Exit codes
    0  every required check passed (warnings are allowed)
    1  at least one required check failed
    2  bad invocation
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Check:
    """One machine or repository check with an actionable detail string."""

    name: str
    status: str
    detail: str
    required: bool = True
    group: str = "core"


def _run(command: list[str], timeout: int = 20) -> tuple[int, str]:
    """Run a small command and return ``(returncode, combined output)``."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, (completed.stdout + completed.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return 127, str(exc)


def _command(name: str) -> str | None:
    """Return the resolved executable path for ``name`` when present."""
    return shutil.which(name)


def _tree_bytes(path: Path) -> int:
    """Measure a tree without following symlinks; stop early on errors."""
    total = 0
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for filename in filenames:
            try:
                total += (Path(directory) / filename).lstat().st_size
            except OSError:
                continue
    return total


def _password_key_present(path: Path) -> bool:
    """Report whether a credential key exists without ever logging its value."""
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"SAMSUNG_NOTES_PASSWORD", "SAM_PASSWORD"}:
            return bool(value.strip().strip("\"'"))
    return False


def _guest_credential() -> tuple[bool, str]:
    """Locate the guest credential: env, native store, then legacy project file.

    The project ``02_extracted_notes_pdf/.env`` is read only when the native
    ``~/.config/sam_win/.env`` has no credential key. No value is ever returned
    or printed; callers receive only presence and the source description.
    """
    if os.environ.get("SAMSUNG_NOTES_PASSWORD") or os.environ.get("SAM_PASSWORD"):
        return True, "environment"
    native = Path.home() / ".config/sam_win/.env"
    if _password_key_present(native):
        return True, str(native)
    legacy = ROOT / "02_extracted_notes_pdf/.env"
    if _password_key_present(legacy):
        return True, str(legacy)
    return False, ""


def _python_import(name: str) -> tuple[bool, str]:
    """Test an import in this interpreter without importing it into the doctor."""
    package_roots = [
        ROOT.parent / "python_libs_gpu/site-packages",
        ROOT.parent / "python_libs/site-packages",
    ]
    package_roots = [str(path) for path in package_roots if path.is_dir()]
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    env["DISABLE_MODEL_SOURCE_CHECK"] = "True"
    if package_roots:
        env["PYTHONPATH"] = os.pathsep.join(package_roots)
    native_roots = [
        ROOT.parent / "python_libs_gpu/site-packages",
        ROOT.parent / "python_libs/site-packages",
    ]
    native_paths = []
    for base in native_roots:
        native_paths.extend(str(path) for pattern in ("nvidia/*/lib", "*.libs")
                            for path in base.glob(pattern) if path.is_dir())
    if native_paths:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            native_paths + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
        )
    code = (
        "import importlib,sys;"
        f"m=importlib.import_module({name!r});"
        "print(getattr(m,'__version__','ok'))"
    )
    if name == "paddle":
        code = (
            "import importlib,sys;"
            f"sys.path.insert(0,{str(ROOT / '00_orchestrator')!r});"
            "m=importlib.import_module('paddle');"
            "from gpu_preflight import check;"
            "print(check(m))"
        )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=45, check=False, env=env,
        )
        code_status = completed.returncode
        output = (completed.stdout + completed.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        code_status, output = 127, str(exc)
    return code_status == 0, output.splitlines()[-1] if output else "unknown"


def _port_open(port: int) -> bool:
    """Return True when a local TCP listener accepts a connection."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _disk_checks() -> list[Check]:
    """Apply the documented workspace cap and volume free-space floor."""
    checks: list[Check] = []
    usage = shutil.disk_usage(ROOT)
    checks.append(
        Check(
            "volume free space",
            "ok" if usage.free >= 15 * 1024**3 else "fail",
            f"{usage.free / 1024**3:.1f} GiB free (floor 15 GiB)",
            group="disk",
        )
    )
    code, output = _run(["du", "-sb", str(ROOT)], 120)
    if code == 0 and output:
        used = int(output.split()[0])
        checks.append(
            Check(
                "workspace size",
                "ok" if used < 55_000_000_000 else "warn",
                f"{used / 1e9:.1f} GB used (documented cap 55 GB)",
                required=False,
                group="disk",
            )
        )
    else:
        checks.append(
            Check(
                "workspace size",
                "skip",
                "du unavailable; run `make manifest` for exact asset totals",
                required=False,
                group="disk",
            )
        )
    return checks


def collect(mode: str) -> list[Check]:
    """Build the full check list for one operating mode."""
    checks: list[Check] = []
    checks.append(
        Check(
            "operating system",
            "ok",
            f"{platform.system()} {platform.release()} ({platform.machine()})",
            required=False,
        )
    )
    version = sys.version_info
    checks.append(
        Check(
            "python",
            "ok" if (3, 10) <= version[:2] < (3, 14) else "warn",
            f"{sys.version.split()[0]} at {sys.executable} (tested 3.10-3.13)",
            required=False,
        )
    )
    checks.append(
        Check(
            "git",
            "ok" if _command("git") else "warn",
            _command("git") or "git not found; repository history is optional at runtime",
            required=False,
        )
    )

    required_dirs = [
        "00_orchestrator",
        "01_vm_windows_sync",
        "02_extracted_notes_pdf",
        "03_rtx_compression_ocr",
        "04_learning_recall_app",
        "05_environments_and_venvs",
    ]
    missing = [name for name in required_dirs if not (ROOT / name).is_dir()]
    checks.append(
        Check(
            "repository layout",
            "ok" if not missing else "fail",
            "all six numbered stages present" if not missing else f"missing: {', '.join(missing)}",
        )
    )

    if mode in {"core", "all"}:
        checks.extend(_disk_checks())
        native_env = Path.home() / ".config/sam_win/.env"
        env_private = native_env.is_file() and not (stat.S_IMODE(native_env.stat().st_mode) & 0o077)
        checks.append(
            Check(
                "secure env file",
                "ok" if env_private else "warn",
                str(native_env)
                + (" present with private mode" if env_private
                   else " absent or group/world-accessible (recommended mode 600)"),
                required=False,
            )
        )

    if mode in {"app", "all"}:
        for module in ("fastapi", "pydantic", "fsrs", "pypdf"):
            ok, detail = _python_import(module)
            checks.append(
                Check(
                    f"python import: {module}",
                    "ok" if ok else "fail",
                    detail,
                    group="app",
                )
            )
        index = ROOT / "04_learning_recall_app/my_notes_graph/data/local_index.sqlite"
        checks.append(
            Check(
                "local retrieval index",
                "ok" if index.is_file() else "warn",
                f"{index.stat().st_size / 1024**2:.0f} MiB" if index.is_file() else "absent; run rebuild_index_offline.sh",
                required=False,
                group="app",
            )
        )
        model = ROOT / "05_environments_and_venvs/models/bge-small-en-v1.5/model.safetensors"
        checks.append(
            Check(
                "semantic model weights",
                "ok" if model.is_file() else "warn",
                f"{model.stat().st_size / 1024**2:.0f} MiB" if model.is_file() else "absent; lexical fallback stays available (`make models`)",
                required=False,
                group="app",
            )
        )
        checks.append(
            Check(
                "dashboard port 8787",
                "ok" if _port_open(8787) else "skip",
                "already listening" if _port_open(8787) else "free",
                required=False,
                group="app",
            )
        )

    if mode in {"export", "all"}:
        cli_candidates = [
            Path(os.environ.get("SAM_SDOCX_CLI", "")),
            ROOT / "tools/sdocx-cli",
            Path.home() / ".local/bin/sdocx-cli",
        ]
        cli = next((p for p in cli_candidates if str(p) and p.is_file()), None)
        checks.append(
            Check(
                "sdocx-cli",
                "ok" if cli else "fail",
                str(cli) if cli else "missing; run `make bootstrap` or set SAM_SDOCX_CLI",
                group="export",
            )
        )
        for command in ("sshpass", "socat", "qemu-system-x86_64"):
            resolved = _command(command)
            checks.append(
                Check(
                    f"command: {command}",
                    "ok" if resolved else "fail",
                    resolved or "not found",
                    group="export",
                )
            )
        disk = ROOT / "01_vm_windows_sync/vm/notes.qcow2"
        checks.append(
            Check(
                "Windows VM disk",
                "ok" if disk.is_file() else "warn",
                f"{disk.stat().st_size / 1e9:.1f} GB" if disk.is_file() else "absent; see 01_vm_windows_sync/README.md",
                required=False,
                group="export",
            )
        )
        present, source = _guest_credential()
        checks.append(
            Check(
                "guest SSH password",
                "ok" if present else "fail",
                f"SAMSUNG_NOTES_PASSWORD present in {source} (value not printed)"
                if present
                else "set SAMSUNG_NOTES_PASSWORD in ~/.config/sam_win/.env",
                group="export",
            )
        )

    if mode in {"ocr", "all"}:
        for module in ("fitz", "pikepdf", "numpy", "paddle"):
            ok, detail = _python_import(module)
            checks.append(
                Check(
                    f"python import: {module}",
                    "ok" if ok else "fail",
                    detail,
                    group="ocr",
                )
            )
        ok, detail = _python_import("paddleocr")
        checks.append(
            Check(
                "python import: paddleocr",
                "ok" if ok else "warn",
                detail if ok else f"{detail}; OCR extras are optional for app-only use",
                required=False,
                group="ocr",
            )
        )
        if _command("nvidia-smi"):
            code, output = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], 20)
            checks.append(
                Check(
                    "CUDA GPU",
                    "ok" if code == 0 else "warn",
                    output.splitlines()[0] if code == 0 and output else "nvidia-smi failed",
                    required=False,
                    group="ocr",
                )
            )
        else:
            checks.append(
                Check(
                    "CUDA GPU",
                    "skip",
                    "nvidia-smi not found; OCR will use CPU",
                    required=False,
                    group="ocr",
                )
            )

    if mode in {"vm", "all"}:
        for path, label in [
            ("/usr/share/OVMF/OVMF_CODE_4M.fd", "OVMF code"),
            ("/usr/share/OVMF/OVMF_VARS_4M.fd", "OVMF vars"),
            ("/dev/kvm", "KVM device"),
        ]:
            candidate = Path(path)
            checks.append(
                Check(
                    label,
                    "ok" if candidate.exists() else "warn",
                    path + (" present" if candidate.exists() else " absent; VM boot may be slow or unavailable"),
                    required=False,
                    group="vm",
                )
            )
        checks.append(
            Check(
                "VM launch script",
                "ok" if (ROOT / "01_vm_windows_sync/run-notes.sh").is_file() else "fail",
                "01_vm_windows_sync/run-notes.sh",
                group="vm",
            )
        )

    return checks


def render(checks: list[Check]) -> str:
    """Format checks as a compact, colour-free terminal report."""
    symbols = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    width = max(len(check.name) for check in checks) if checks else 10
    lines = [f"Samsung Notes workspace doctor  ({ROOT})", ""]
    for check in checks:
        lines.append(f"  [{symbols.get(check.status, 'INFO')}] {check.name:<{width}}  {check.detail}")
    failures = [check for check in checks if check.status == "fail" and check.required]
    warnings = [check for check in checks if check.status in {"warn", "fail"} and not (check.status == "fail" and check.required)]
    lines.append("")
    lines.append(f"{len(checks)} checks: {len(checks) - len(failures) - len(warnings)} passed, {len(warnings)} warning(s), {len(failures)} failure(s)")
    if failures:
        lines.append("Fix the FAIL rows above, then re-run: python scripts/doctor.py --mode " + "all")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the checks and emit text or JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("core", "app", "export", "ocr", "vm", "all"),
        default="all",
        help="component to verify",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="downgrade required failures to warnings (useful in CI or cold clones)",
    )
    args = parser.parse_args(argv)

    checks = collect(args.mode)
    if args.allow_missing:
        for check in checks:
            if check.status == "fail":
                check.status = "warn"
                check.detail += " [allowed in this mode]"

    failures = [check for check in checks if check.status == "fail" and check.required]
    payload = {
        "schema": 1,
        "root": str(ROOT),
        "mode": args.mode,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "summary": {
            "total": len(checks),
            "failed": len(failures),
            "warnings": sum(check.status == "warn" for check in checks),
        },
        "checks": [asdict(check) for check in checks],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(checks))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
