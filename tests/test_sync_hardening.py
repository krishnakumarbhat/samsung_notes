"""Focused regression tests for orchestrator/VM disk policy and entry points.

All dependencies are faked; no VM, guest sync, exporter or real desktop entry is
touched. The monitor is bounded with ``timeout`` and a no-op ``sleep``.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(0o755)


def _run(cmd: list[str], env: dict[str, str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- #
# GPU preflight (real python module, fake paddle)
# --------------------------------------------------------------------------- #
def _gpu_module():
    path = ROOT / "00_orchestrator/gpu_preflight.py"
    spec = importlib.util.spec_from_file_location("gpu_preflight_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, value: float) -> None:
        self.value = value

    def numpy(self):
        return SimpleNamespace(item=lambda: self.value)


class _Tensor:
    def __init__(self, gpu_place: bool, value: float) -> None:
        self.place = SimpleNamespace(is_gpu_place=lambda: gpu_place)
        self.value = value

    def __mul__(self, other: float):
        return _Result(self.value * other)


def _paddle(*, built: bool = True, count: int = 1, gpu_place: bool = True, value: float = 1.0):
    tensor = _Tensor(gpu_place, value)
    return SimpleNamespace(
        is_compiled_with_cuda=lambda: built,
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: count)),
        CUDAPlace=lambda index: SimpleNamespace(index=index),
        to_tensor=lambda values, place: tensor,
    )


def test_gpu_preflight_passes_only_with_real_cuda_op() -> None:
    module = _gpu_module()
    assert "GPU OK" in module.check(_paddle())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"built": False},
        {"count": 0},
        {"gpu_place": False},
        {"value": 9.0},
    ],
)
def test_gpu_preflight_fails_closed(kwargs: dict[str, object]) -> None:
    module = _gpu_module()
    with pytest.raises(RuntimeError):
        module.check(_paddle(**kwargs))  # type: ignore[arg-type]


def test_gpu_preflight_main_returns_nonzero_on_failure() -> None:
    module = _gpu_module()
    assert module.main(_paddle(count=0)) == 1


# --------------------------------------------------------------------------- #
# Orchestrator pipeline sandbox
# --------------------------------------------------------------------------- #
def _pipeline_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    scripts = tmp_path / "00_orchestrator"
    scripts.mkdir()
    runner = scripts / "sync_and_export_notes.sh"
    shutil.copyfile(ROOT / "00_orchestrator/sync_and_export_notes.sh", runner)
    events = tmp_path / "events"
    events.write_text("")
    (scripts / "runtime.sh").write_text(
        'SAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"\n'
        "export SAM_ROOT\nOPENCODE_PYTHON=\"$SAM_ROOT/fake-python\"\n"
        'gpu_preflight() { return "${GPU_STATUS:-0}"; }\n'
    )
    _executable(tmp_path / "fake-python", '''
if [[ "${1:-}" == -c ]]; then exit 0; fi
if [[ "${1:-}" == - ]]; then
  cat >/dev/null; echo index >>"$EVENTS"; echo '{"unreadable":0}'; exit 0
fi
if [[ -f "$SAM_ROOT/temp/export_active.flag" ]]; then
  read -r _ ph < "$SAM_ROOT/temp/export_active.flag" || ph="?"
  echo "phase-$ph" >>"$EVENTS"
fi
if [[ "$*" == *incremental_export.py* && "${EXPORT_STATUS:-0}" != 0 ]]; then
  exit "$EXPORT_STATUS"
fi
echo python >>"$EVENTS"
exit 0
''')
    _executable(tmp_path / "01_vm_windows_sync/run-notes.sh", '''
case "$1" in
  status) if [[ -f "$SAM_ROOT/vm-alive" ]]; then echo running; else echo stopped; fi ;;
  start) echo start >>"$EVENTS"; touch "$SAM_ROOT/vm-alive" ;;
  stop) echo stop >>"$EVENTS"; rm -f "$SAM_ROOT/vm-alive" ;;
  *) echo "$1" >>"$EVENTS" ;;
esac
''')
    _executable(tmp_path / "01_vm_windows_sync/guestsetup/sync_now.sh", '''
if [[ -f "$SAM_ROOT/temp/export_active.flag" ]]; then
  read -r _ ph < "$SAM_ROOT/temp/export_active.flag" || ph="?"
  echo "sync-phase-$ph" >>"$EVENTS"
fi
echo sync >>"$EVENTS"
exit "${SYNC_STATUS:-0}"
''')
    _executable(tmp_path / "04_learning_recall_app/my_notes_graph/run_app.sh",
                'if [[ -e "/proc/$$/fd/9" ]]; then echo app-lock-inherited >>"$EVENTS"; '
                'else echo app-fd-closed >>"$EVENTS"; fi\n')
    (tmp_path / "02_extracted_notes_pdf/script").mkdir(parents=True)
    (tmp_path / "02_extracted_notes_pdf/script/incremental_export.py").write_text("")
    tools = tmp_path / "bin"
    _executable(tools / "du", '''
if [[ "${DU_FAIL:-0}" == 1 ]]; then echo "du: failed" >&2; exit 1; fi
printf "%s\\t%s\\n" "${DU_BYTES:-52000000000}" "${@: -1}"
''')
    _executable(tools / "df", 'printf "Avail\\n%s\\n" "${FREE_BYTES:-40000000000}"\n')
    _executable(tools / "curl", 'echo "{\\"ok\\":true}"\n')
    home = tmp_path / "home"
    _executable(home / ".local/bin/sdocx-cli", "exit 0\n")
    env = dict(os.environ, EVENTS=str(events), HOME=str(home),
               PATH=str(tools) + os.pathsep + os.environ["PATH"])
    return runner, env, events


def test_disk_over_cap_refuses_without_booting(tmp_path: Path) -> None:
    runner, env, events = _pipeline_sandbox(tmp_path)
    env["DU_BYTES"] = "56000000000"
    result = _run(["bash", str(runner)], env)
    assert result.returncode != 0
    assert "start" not in events.read_text().splitlines()
    assert "Disk guard refused" in result.stderr


def test_unmeasurable_disk_refuses_without_booting(tmp_path: Path) -> None:
    runner, env, events = _pipeline_sandbox(tmp_path)
    env["DU_FAIL"] = "1"
    result = _run(["bash", str(runner)], env)
    assert result.returncode != 0
    assert "start" not in events.read_text().splitlines()
    assert "could not measure" in result.stderr


def test_phase_flag_covers_sync_then_export_and_index_runs_once(tmp_path: Path) -> None:
    runner, env, events = _pipeline_sandbox(tmp_path)
    result = _run(["bash", str(runner)], env)
    log = events.read_text().splitlines()
    assert result.returncode == 0, result.stderr
    assert log.index("sync-phase-sync") < log.index("sync"), log
    assert log.count("phase-export") == 1, log
    assert log.count("index") == 1, log
    assert "app-fd-closed" in log and "app-lock-inherited" not in log, log
    assert not (tmp_path / "temp/export_active.flag").exists()


def test_export_failure_indexes_once_and_releases_flag(tmp_path: Path) -> None:
    runner, env, events = _pipeline_sandbox(tmp_path)
    env["EXPORT_STATUS"] = "3"
    result = _run(["bash", str(runner)], env)
    log = events.read_text().splitlines()
    assert result.returncode != 0
    assert log.count("index") == 1, log
    assert log.count("start") == 1 and log.count("stop") == 1, log
    assert "app-fd-closed" in log and "app-lock-inherited" not in log, log
    assert not (tmp_path / "temp/export_active.flag").exists()


# --------------------------------------------------------------------------- #
# VM disk monitor policy
# --------------------------------------------------------------------------- #
def _vm_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    scripts = tmp_path / "01_vm_windows_sync"
    scripts.mkdir()
    runner = scripts / "run-notes.sh"
    shutil.copyfile(ROOT / "01_vm_windows_sync/run-notes.sh", runner)
    (scripts / "vm").mkdir()
    (scripts / "vm/notes.qcow2").write_bytes(b"")
    (tmp_path / "temp").mkdir()
    log = tmp_path / "socat.log"
    tools = tmp_path / "bin"
    _executable(tools / "du", '''
if [[ "${DU_FAIL:-0}" == 1 ]]; then echo "du: failed" >&2; exit 1; fi
case "${@: -1}" in
  *notes.qcow2) printf "%s\\t%s\\n" "${QCOW_BYTES:-55000000000}" "${@: -1}" ;;
  *) printf "%s\\t%s\\n" "${WS_BYTES:-53000000000}" "${@: -1}" ;;
esac
''')
    _executable(tools / "df", '''
if [[ "${DF_FAIL:-0}" == 1 ]]; then echo "df: failed" >&2; exit 1; fi
printf "Avail\\n%s\\n" "${FREE_BYTES:-40000000000}"
''')
    _executable(tools / "sleep", "exit 0\n")
    _executable(tools / "socat", 'echo socat >>"$SOCAT_LOG"\n')
    env = dict(os.environ, SOCAT_LOG=str(log),
               PATH=str(tools) + os.pathsep + os.environ["PATH"])
    return runner, env, log


def _monitor_once(runner: Path, env: dict[str, str]) -> tuple[int, str]:
    guest = subprocess.Popen(["/bin/sleep", "30"])
    try:
        result = _run(["timeout", "2", "bash", str(runner), "monitor-worker",
                       str(guest.pid)], env, timeout=6)
        return result.returncode, result.stderr
    finally:
        guest.terminate()
        guest.wait(timeout=5)


def _set_phase(tmp_path: Path, phase: str, pid: int | None = None) -> None:
    (tmp_path / "temp/export_active.flag").write_text(f"{pid or os.getpid()} {phase}\n")


def test_monitor_defers_both_thresholds_during_export(tmp_path: Path) -> None:
    runner, env, _ = _vm_sandbox(tmp_path)
    _set_phase(tmp_path, "export")
    env["QCOW_BYTES"] = "55000000000"   # qcow 54 GB early threshold
    env["WS_BYTES"] = "56000000000"     # workspace 55 GB hard cap
    _, stderr = _monitor_once(runner, env)
    assert "DEFERRED[vm-qcow2]" in stderr, stderr
    assert "requesting graceful VM shutdown" not in stderr, stderr
    # The monitor skips the workspace scan during export (FUSE cost); invoking
    # the same check directly must still defer rather than request a shutdown.
    cap = _run(["bash", str(runner), "check-disk"], env)
    assert cap.returncode == 1, cap.stderr
    assert "DEFERRED[workspace]" in cap.stderr, cap.stderr


def test_monitor_enforces_workspace_cap_during_sync(tmp_path: Path) -> None:
    runner, env, _ = _vm_sandbox(tmp_path)
    _set_phase(tmp_path, "sync")
    env["QCOW_BYTES"] = "1000000000"
    env["WS_BYTES"] = "56000000000"
    rc, stderr = _monitor_once(runner, env)
    assert "CRITICAL[workspace]" in stderr, stderr
    assert "requesting graceful VM shutdown" in stderr, stderr
    assert rc == 2, rc


def test_monitor_defers_qcow_warning_but_keeps_cap_during_sync(tmp_path: Path) -> None:
    """qcow 54 GB and workspace 55 GB are complementary, not contradictory."""
    runner, env, _ = _vm_sandbox(tmp_path)
    _set_phase(tmp_path, "sync")
    env["QCOW_BYTES"] = "54500000000"   # above qcow threshold, below hard cap
    env["WS_BYTES"] = "54800000000"     # still below the 55 GB cap
    rc, stderr = _monitor_once(runner, env)
    assert "DEFERRED[vm-qcow2]" in stderr, stderr
    assert "CRITICAL[workspace]" not in stderr, stderr
    assert rc != 2, rc
    # Same state checked as whole workspace: headroom only, no shutdown.
    cap = _run(["bash", str(runner), "check-disk"], env)
    assert cap.returncode == 1, cap.stderr
    assert "HEADROOM[workspace]" in cap.stderr, cap.stderr


def test_monitor_fails_closed_when_du_unmeasurable(tmp_path: Path) -> None:
    runner, env, _ = _vm_sandbox(tmp_path)
    env["DU_FAIL"] = "1"
    rc, stderr = _monitor_once(runner, env)
    assert "measurement failed" in stderr, stderr
    assert "requesting graceful VM shutdown" in stderr, stderr
    assert rc == 2, rc


def test_monitor_defers_unmeasurable_disk_during_export(tmp_path: Path) -> None:
    runner, env, _ = _vm_sandbox(tmp_path)
    _set_phase(tmp_path, "export")
    env["DU_FAIL"] = "1"
    env["DF_FAIL"] = "1"
    _, stderr = _monitor_once(runner, env)
    assert "DEFERRED[vm-qcow2]: measurement failed" in stderr, stderr
    assert "requesting graceful VM shutdown" not in stderr, stderr


def test_monitor_shuts_down_unowned_qcow_over_threshold(tmp_path: Path) -> None:
    runner, env, _ = _vm_sandbox(tmp_path)
    rc, stderr = _monitor_once(runner, env)
    assert "CRITICAL[vm-qcow2]" in stderr, stderr
    assert "requesting graceful VM shutdown" in stderr, stderr
    assert rc == 2, rc


def test_monitor_ignores_stale_phase_flag(tmp_path: Path) -> None:
    runner, env, _ = _vm_sandbox(tmp_path)
    _set_phase(tmp_path, "export", pid=99999999)
    rc, stderr = _monitor_once(runner, env)
    assert "requesting graceful VM shutdown" in stderr, stderr
    assert rc == 2, rc


def test_monitor_stop_never_signals_unrelated_process(tmp_path: Path) -> None:
    """A PID in disk_monitor.pid is signalled only with a verified identity."""
    runner, env, _ = _vm_sandbox(tmp_path)
    scripts = runner.parent
    guest = subprocess.Popen(["/bin/sleep", "30"])
    unrelated = subprocess.Popen(["/bin/sleep", "30"])
    try:
        (scripts / "vm/qemu.pid").write_text(f"{guest.pid}\n")
        (scripts / "vm/disk_monitor.pid").write_text(
            f"{unrelated.pid} {guest.pid}\n")
        _executable(tmp_path / "bin/ps",
                    'echo "/usr/bin/qemu-system-x86_64 /dummy/notes.qcow2"\n')
        result = _run(["bash", str(runner), "monitor-stop"], env)
        assert result.returncode == 0, result.stderr
        assert unrelated.poll() is None, "unrelated PID was signalled"
    finally:
        guest.terminate()
        unrelated.terminate()
        guest.wait(timeout=5)
        unrelated.wait(timeout=5)


# --------------------------------------------------------------------------- #
# Desktop entry points and installer
# --------------------------------------------------------------------------- #
def test_launcher_resolves_current_root_and_forwards_args(tmp_path: Path) -> None:
    scripts = tmp_path / "proj/00_orchestrator"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "00_orchestrator/launch_sync.sh", scripts / "launch_sync.sh")
    events = tmp_path / "events"
    events.write_text("")
    _executable(scripts / "sync_button.sh",
                'cd "$(dirname "$0")/.."\necho "$PWD" >>"$EVENTS"\necho "$*" >>"$EVENTS"\n')
    env = dict(os.environ, EVENTS=str(events))
    result = _run(["bash", str(scripts / "launch_sync.sh"), "--dry-run", "extra"], env)
    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == [str(scripts.parent), "--dry-run extra"]


def test_button_uses_workspace_lock_log_and_forwards_args(tmp_path: Path) -> None:
    scripts = tmp_path / "proj/00_orchestrator"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "00_orchestrator/sync_button.sh", scripts / "sync_button.sh")
    events = tmp_path / "events"
    events.write_text("")
    _executable(scripts / "sync_and_export_notes.sh",
                'echo "$*" >>"$EVENTS"\n')
    home = tmp_path / "home"
    env = dict(os.environ, EVENTS=str(events), HOME=str(home))
    result = _run(["bash", str(scripts / "sync_button.sh"), "--dry-run"], env)
    assert result.returncode == 0, result.stderr
    history = scripts.parent / "02_extracted_notes_pdf/ai_agend_history"
    assert (history / "button.lock").exists()
    logs = list(history.glob("orchestrator_run_*.log"))
    assert len(logs) == 1
    assert "workspace:" in logs[0].read_text()
    assert events.read_text().splitlines() == ["--dry-run"]


def test_installer_dry_run_and_idempotent_install(tmp_path: Path) -> None:
    scripts = tmp_path / "proj/00_orchestrator"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "00_orchestrator/install_sync_desktop.sh",
                    scripts / "install_sync_desktop.sh")
    _executable(scripts / "launch_sync.sh", "exit 0\n")
    desktop = tmp_path / "desktop"
    env = dict(os.environ, DESKTOP_DIR=str(desktop))

    dry = _run(["bash", str(scripts / "install_sync_desktop.sh"), "--dry-run"], env)
    assert dry.returncode == 0, dry.stderr
    assert f'Exec="{scripts / "launch_sync.sh"}"' in dry.stdout
    assert "Terminal=true" in dry.stdout
    assert not list(desktop.glob("*.desktop"))

    command = ["bash", str(scripts / "install_sync_desktop.sh")]
    assert _run(command, env).returncode == 0
    target = desktop / "Samsung Notes Sync.desktop"
    first = target.read_text()
    assert _run(command, env).returncode == 0
    assert target.read_text() == first  # idempotent
    assert f'Exec="{scripts / "launch_sync.sh"}"' in first
    assert "Terminal=true" in first
