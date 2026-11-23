#!/usr/bin/env python3
"""Reclaim abandoned ``_run_tmp_*`` scratch directories, never live ones.

Scratch lives as a direct child of ``sam_notes`` named
``_run_tmp_<pid>[.<suffix>]``. A run is considered live while any of these
hold, checked in order:

* its ``.owner.lock`` is held under an exclusive ``flock``;
* the pid encoded in its name is still running;
* its directory mtime is younger than the stale threshold.

Symlinks are never followed, only real directories are removed, and every
removal is confirmed to resolve inside the notes root. ``private_state`` and
any non-prefixed entry are outside the candidate set by construction.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PREFIX = "_run_tmp_"
LOCK_NAME = ".owner.lock"
DEFAULT_STALE_SECONDS = 6 * 60 * 60
PROTECTED_NAMES = frozenset({"private_state"})
_OWNER = re.compile(rf"^{re.escape(PREFIX)}(\d+)(?:\..*)?$")


@dataclass(frozen=True)
class Entry:
    """One candidate and the decision taken for it."""

    path: str
    action: str
    reason: str


def _within(path: Path, root: Path) -> bool:
    """True when ``path`` is strictly inside ``root`` after resolution."""
    try:
        return path.resolve().is_relative_to(root.resolve()) and path.resolve() != root.resolve()
    except (OSError, ValueError):
        return False


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` names a running process this user can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def owner_pid(name: str) -> int | None:
    """Return the pid encoded in a run directory name, if any."""
    match = _OWNER.match(name)
    return int(match.group(1)) if match else None


def lock_held(directory: Path) -> bool:
    """True when another process holds this run's exclusive owner lock."""
    try:
        descriptor = os.open(directory / LOCK_NAME, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        return error.errno in (errno.EACCES, errno.EAGAIN)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _candidate(entry: Path, root: Path) -> bool:
    """Only real, directly nested ``_run_tmp_<pid>`` names are candidates."""
    return (
        entry.parent == root
        and entry.name.startswith(PREFIX)
        and entry.name not in PROTECTED_NAMES
        and owner_pid(entry.name) is not None
        and (entry.is_dir() or entry.is_symlink())
    )


def sweep(notes_root: Path | str, *, stale_after: float = DEFAULT_STALE_SECONDS,
          exclude_pids: set[int] | frozenset[int] = frozenset(),
          dry_run: bool = False, now: float | None = None) -> list[Entry]:
    """Classify and optionally remove abandoned run directories.

    Returns one :class:`Entry` per candidate with the decision and reason. A
    symlinked or escaping entry is reported and left untouched, so a hostile
    link can never redirect deletion outside ``notes_root``.
    """
    root = Path(notes_root).expanduser()
    if root.is_symlink():
        raise ValueError(f"notes root must not be a symlink: {root}")
    root = root.resolve()
    if not root.is_dir():
        return []
    moment = time.time() if now is None else now
    decisions: list[Entry] = []
    for entry in sorted(root.iterdir()):
        if not _candidate(entry, root):
            continue
        if entry.is_symlink():
            decisions.append(Entry(str(entry), "skipped_symlink",
                                   "symlink entries are never followed or removed"))
            continue
        if not entry.is_dir() or not _within(entry, root):
            decisions.append(Entry(str(entry), "skipped_unsafe",
                                   "entry is not a real directory inside the notes root"))
            continue
        pid = owner_pid(entry.name)
        if pid in exclude_pids:
            decisions.append(Entry(str(entry), "kept_live", "owner pid excluded by caller"))
            continue
        if lock_held(entry):
            decisions.append(Entry(str(entry), "kept_live", "owner lock is held"))
            continue
        if pid is not None and _pid_alive(pid):
            decisions.append(Entry(str(entry), "kept_live", "owner pid is still running"))
            continue
        try:
            age = moment - entry.stat().st_mtime
        except OSError:
            age = 0.0
        if age < stale_after:
            decisions.append(Entry(str(entry), "kept_fresh", "younger than the stale threshold"))
            continue
        if dry_run:
            decisions.append(Entry(str(entry), "would_remove", "stale and unowned"))
            continue
        shutil.rmtree(entry)
        decisions.append(Entry(str(entry), "removed", "stale and unowned"))
    return decisions


def remove_owned_run_tmp(path: Path | str, pid: int, notes_root: Path | str) -> bool:
    """Remove ``path`` only when this exact pid owns it and it is a real dir.

    Every check fails closed: a symlink, a mismatched owner pid, a protected
    name, or a path that resolves outside the notes root is refused.
    """
    root = Path(notes_root).expanduser()
    if root.is_symlink():
        return False
    root = root.resolve()
    entry = Path(path).expanduser()
    if entry.is_symlink() or entry.name in PROTECTED_NAMES:
        return False
    if not entry.name.startswith(PREFIX) or entry.parent != root:
        return False
    if owner_pid(entry.name) != pid:
        return False
    if not entry.is_dir() or not _within(entry, root):
        return False
    shutil.rmtree(entry)
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: sweep ``--notes-root`` or remove ``--own``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--notes-root", type=Path, required=True)
    parser.add_argument("--own", type=Path,
                        help="remove this run directory when --own-pid matches")
    parser.add_argument("--own-pid", type=int, default=os.getpid())
    parser.add_argument("--stale-after-seconds", type=float, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--exclude-pid", type=int, action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit the decisions as JSON")
    args = parser.parse_args(argv)

    if args.own is not None:
        removed = remove_owned_run_tmp(args.own, args.own_pid, args.notes_root)
        if args.json:
            print(json.dumps({"path": str(args.own), "removed": removed}))
        return 0

    try:
        decisions = sweep(args.notes_root, stale_after=args.stale_after_seconds,
                          exclude_pids=set(args.exclude_pid) | {args.own_pid},
                          dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        print(f"run_tmp_cleanup: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([asdict(item) for item in decisions], indent=2))
    else:
        for item in decisions:
            print(f"{item.action}\t{item.path}\t{item.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
