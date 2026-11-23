"""Safety checks for reclaiming abandoned ``_run_tmp_*`` scratch directories."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import run_tmp_cleanup as cleaner

ROOT = Path(__file__).resolve().parents[1]
DEAD_PID = 2_147_483_647  # above the Linux pid ceiling, so never running
OLD = 10 * 60 * 60  # seconds


def _run(root: Path, pid: int = DEAD_PID, age: float = OLD, suffix: str = "") -> Path:
    """Create a candidate directory with a controlled age."""
    path = root / f"_run_tmp_{pid}{suffix}"
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(b"x")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def _actions(decisions) -> dict[str, str]:
    return {Path(item.path).name: item.action for item in decisions}


def test_removes_only_stale_unowned_run(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    stale = _run(notes)
    live = _run(notes, pid=os.getpid())
    fresh = _run(notes, suffix=".fresh", age=5)
    decisions = cleaner.sweep(notes)
    actions = _actions(decisions)
    assert actions[stale.name] == "removed"
    assert not stale.exists()
    assert live.is_dir()
    assert fresh.is_dir()
    assert actions[live.name] == "kept_live"
    assert actions[fresh.name] == "kept_fresh"


def test_held_owner_lock_protects_an_old_directory(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    guarded = _run(notes)
    descriptor = os.open(guarded / cleaner.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        decisions = cleaner.sweep(notes)
        assert _actions(decisions)[guarded.name] == "kept_live"
        assert guarded.is_dir()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert _actions(cleaner.sweep(notes, stale_after=0))[guarded.name] == "removed"


def test_symlink_is_never_followed_or_removed(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("important")
    link = notes / f"_run_tmp_{DEAD_PID}"
    link.symlink_to(target, target_is_directory=True)
    stamp = time.time() - OLD
    os.utime(link, (stamp, stamp), follow_symlinks=False)
    decisions = cleaner.sweep(notes)
    assert _actions(decisions)[link.name] == "skipped_symlink"
    assert link.is_symlink()
    assert (target / "keep.txt").read_text() == "important"


def test_private_state_and_unrelated_entries_are_never_candidates(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    private = notes / "private_state"
    private.mkdir()
    (private / "token").write_text("secret")
    plain = notes / "notes"
    plain.mkdir()
    loose_file = notes / f"_run_tmp_{DEAD_PID}.zip"
    loose_file.write_text("not a directory")
    old = time.time() - OLD
    for path in (private, plain, loose_file):
        os.utime(path, (old, old))
    decisions = cleaner.sweep(notes)
    assert decisions == []
    assert (private / "token").read_text() == "secret"
    assert plain.is_dir() and loose_file.is_file()


def test_dry_run_reports_without_deleting(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    stale = _run(notes)
    decisions = cleaner.sweep(notes, dry_run=True)
    assert _actions(decisions)[stale.name] == "would_remove"
    assert stale.is_dir()


def test_excluded_pid_is_treated_as_live(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    owned = _run(notes, pid=os.getpid())
    assert _actions(cleaner.sweep(notes))[owned.name] == "kept_live"


def test_owned_removal_refuses_mismatched_pid_and_foreign_paths(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    mine = _run(notes, pid=os.getpid())
    assert not cleaner.remove_owned_run_tmp(mine, os.getpid() + 1, notes)
    assert mine.is_dir()
    outside = _run(tmp_path)
    assert not cleaner.remove_owned_run_tmp(outside, DEAD_PID, notes)
    assert outside.is_dir()
    assert cleaner.remove_owned_run_tmp(mine, os.getpid(), notes)
    assert not mine.exists()


def test_owned_removal_refuses_symlinked_entry(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    target = tmp_path / "real"
    target.mkdir()
    link = notes / f"_run_tmp_{DEAD_PID}"
    link.symlink_to(target, target_is_directory=True)
    assert not cleaner.remove_owned_run_tmp(link, DEAD_PID, notes)
    assert target.is_dir()


def test_symlinked_notes_root_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        cleaner.sweep(alias)


def test_cli_emits_json_and_removes(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    stale = _run(notes)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_tmp_cleanup.py"),
         "--notes-root", str(notes), "--json"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    assert [item["action"] for item in payload] == ["removed"]
    assert not stale.exists()


def test_exporter_sigterm_removes_its_own_scratch(tmp_path):
    notes = tmp_path / "sam_notes"
    notes.mkdir()
    script = ROOT / "02_extracted_notes_pdf/script/incremental_export.py"
    probe = (
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(script.parent)!r})\n"
        "import incremental_export as ie\n"
        "ie._install_run_tmp_lifecycle()\n"
        "ie.DEFAULT_RUN_ROOT.mkdir(parents=True, exist_ok=True)\n"
        "(ie.DEFAULT_RUN_ROOT / 'stage.bin').write_bytes(b'x')\n"
        "print(ie.DEFAULT_RUN_ROOT, flush=True)\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"SN_EXPORT_WORK", "SAM_RUN_TMP"}}
    environment["SAM_NOTES_ROOT"] = str(notes)
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, env=environment, timeout=60)
    assert result.returncode == -15, result.stderr
    created = Path(result.stdout.strip())
    assert created.parent == notes
    assert not created.exists()


def test_runtime_exit_cleans_owned_scratch_without_touching_private_state(tmp_path):
    project = tmp_path / "sam_win"
    (project / "00_orchestrator").mkdir(parents=True)
    (project / "scripts").mkdir()
    shutil.copyfile(ROOT / "00_orchestrator/runtime.sh", project / "00_orchestrator/runtime.sh")
    shutil.copyfile(ROOT / "scripts/run_tmp_cleanup.py", project / "scripts/run_tmp_cleanup.py")
    private = tmp_path / "sam_notes/private_state"
    private.mkdir(parents=True)
    (private / "keep").write_text("preserved")
    env = {k: v for k, v in os.environ.items()
           if k not in {"SAM_RUN_TMP", "SAM_RUN_TMP_OWNER_PID", "SN_EXPORT_WORK", "SN_PAGE_CACHE_DIR"}}
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; echo "$SAM_RUN_TMP"', "bash",
         str(project / "00_orchestrator/runtime.sh")],
        env=env, text=True, capture_output=True, check=True, timeout=30,
    )
    assert not Path(result.stdout.strip()).exists()
    assert (private / "keep").read_text() == "preserved"


def test_runtime_and_orchestrator_wire_the_cleanup():
    runtime = (ROOT / "00_orchestrator/runtime.sh").read_text()
    orchestrator = (ROOT / "00_orchestrator/sync_and_export_notes.sh").read_text()
    exporter = (ROOT / "02_extracted_notes_pdf/script/incremental_export.py").read_text()
    assert "run_tmp_cleanup.py" in runtime
    assert "trap 'sam_cleanup_run_tmp; exit 130' INT" in runtime
    assert "trap 'sam_cleanup_run_tmp; exit 143' TERM" in runtime
    assert "sam_sweep_run_tmp" in runtime and "sam_sweep_run_tmp" in orchestrator
    assert "_install_run_tmp_lifecycle" in exporter
    assert "signal.SIGTERM" in exporter
