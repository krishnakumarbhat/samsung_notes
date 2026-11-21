"""Exercise the orchestrator with fake VM/GPU commands, never a real guest."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _executable(path: Path, text: str) -> None:
    """Write a deterministic isolated executable fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(0o755)


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Copy the shell entry point but replace every mutating dependency."""
    scripts = tmp_path / "00_orchestrator"
    scripts.mkdir()
    runner = scripts / "sync_and_export_notes.sh"
    shutil.copyfile(ROOT / "00_orchestrator/sync_and_export_notes.sh", runner)
    events = tmp_path / "events"
    events.write_text("")
    runtime = '''SAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SAM_ROOT
OPENCODE_PYTHON="$SAM_ROOT/fake-python"
gpu_preflight() { return "${GPU_STATUS:-0}"; }
'''
    (scripts / "runtime.sh").write_text(runtime)
    _executable(tmp_path / "fake-python", '''
if [[ "${1:-}" == -c ]]; then
  if [[ "$*" == *paddle* && "${GPU_STATUS:-0}" != 0 ]]; then exit "$GPU_STATUS"; fi
  if [[ "$*" == *CLI* ]]; then echo "$SAM_ROOT/fake-cli"; fi
  exit 0
fi
if [[ "${1:-}" == - ]]; then cat >/dev/null; echo '{"unreadable":0}'; fi
echo python >>"$EVENTS"
exit 0
''')
    _executable(tmp_path / "fake-cli", "exit 0\n")
    _executable(tmp_path / "01_vm_windows_sync/run-notes.sh", '''
case "$1" in
  status) if [[ -f "$SAM_ROOT/vm-alive" ]]; then echo running; else echo stopped; fi ;;
  start) echo start >>"$EVENTS"; touch "$SAM_ROOT/vm-alive" ;;
  stop) echo stop >>"$EVENTS"; rm -f "$SAM_ROOT/vm-alive" ;;
  *) echo "$1" >>"$EVENTS" ;;
esac
''')
    _executable(tmp_path / "01_vm_windows_sync/guestsetup/sync_now.sh", '''
echo sync >>"$EVENTS"
exit "${SYNC_STATUS:-0}"
''')
    _executable(tmp_path / "04_learning_recall_app/my_notes_graph/run_app.sh",
                'echo app >>"$EVENTS"\n')
    (tmp_path / "02_extracted_notes_pdf/script").mkdir(parents=True)
    (tmp_path / "02_extracted_notes_pdf/script/incremental_export.py").write_text("")
    tools = tmp_path / "bin"
    _executable(tools / "du", 'printf "52000000000\\t%s\\n" "$SAM_ROOT"\n')
    _executable(tools / "df", 'printf "Avail\\n50000000000\\n"\n')
    _executable(tools / "curl", 'echo "{\\"ok\\":true}"\n')
    home = tmp_path / "home"
    _executable(home / ".local/bin/sdocx-cli", "exit 0\n")
    env = dict(os.environ, EVENTS=str(events), HOME=str(home),
               PATH=str(tools) + os.pathsep + os.environ["PATH"],
               SAM_SDOCX_CLI=str(tmp_path / "fake-cli"))
    return runner, env, events


def test_dry_run_never_boots_or_syncs(tmp_path: Path) -> None:
    """A launcher dry run is safe even with no working GPU."""
    runner, env, events = _sandbox(tmp_path)
    env["GPU_STATUS"] = "7"
    result = subprocess.run(["bash", str(runner), "--dry-run"], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text() == ""


def test_failed_gpu_preflight_never_boots_vm(tmp_path: Path) -> None:
    """An explicit GPU failure is surfaced, not silently switched to CPU."""
    runner, env, events = _sandbox(tmp_path)
    env["GPU_STATUS"] = "7"
    result = subprocess.run(["bash", str(runner)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "start" not in events.read_text().splitlines()


def test_guest_sync_failure_stops_only_owned_vm(tmp_path: Path) -> None:
    """A failed guest sync still shuts down a VM owned by this invocation."""
    runner, env, events = _sandbox(tmp_path)
    env["SYNC_STATUS"] = "8"
    result = subprocess.run(["bash", str(runner)], env=env,
                            capture_output=True, text=True, timeout=15)
    log = events.read_text().splitlines()
    assert result.returncode != 0
    assert log.count("start") == 1, (log, result.stdout, result.stderr)
    assert log.count("sync") == 1
    assert log.count("stop") == 1


def test_guest_sync_failure_preserves_preexisting_vm(tmp_path: Path) -> None:
    """The pipeline must not shut down a VM the user already had running."""
    runner, env, events = _sandbox(tmp_path)
    (tmp_path / "vm-alive").touch()
    env["SYNC_STATUS"] = "8"
    result = subprocess.run(["bash", str(runner)], env=env,
                            capture_output=True, text=True, timeout=15)
    log = events.read_text().splitlines()
    assert result.returncode != 0
    assert "sync" in log, (log, result.stdout, result.stderr)
    assert "start" not in log
    assert "stop" not in log
