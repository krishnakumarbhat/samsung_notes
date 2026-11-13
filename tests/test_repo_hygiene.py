"""Pre-push hygiene tests: sizes, secrets, syntax, links and replication files."""
from __future__ import annotations

from pathlib import Path

from scripts import verify_repo

ROOT = Path(__file__).resolve().parents[1]


def test_no_tracked_file_reaches_github_limit():
    """GitHub rejects files over 100 MB; the repository budget is 99 MB."""
    findings = verify_repo.check_sizes(ROOT, verify_repo.tracked_files(ROOT))
    failed = [item for item in findings if item.status == "fail"]
    assert not failed, failed[0].detail


def test_no_local_note_payloads_or_machine_environments_are_tracked():
    """Private notes, VM state and native environments must remain local."""
    findings = verify_repo.check_private_payloads(ROOT, verify_repo.tracked_files(ROOT))
    failed = [item for item in findings if item.status == "fail"]
    assert not failed, failed[0].detail


def test_no_environment_file_or_credential_is_tracked():
    """Secrets stay in ~/.config/sam_win/.env or an untracked local .env."""
    findings = verify_repo.check_env_and_secrets(ROOT, verify_repo.tracked_files(ROOT))
    failed = [item for item in findings if item.status == "fail"]
    assert not failed, failed[0].detail


def test_tracked_json_and_python_are_valid():
    """A broken report or syntax error must fail before the push."""
    files = verify_repo.tracked_files(ROOT)
    for check in (verify_repo.check_json, verify_repo.check_python, verify_repo.check_shell):
        findings = check(ROOT, files)
        failed = [item for item in findings if item.status == "fail"]
        assert not failed, failed[0].detail


def test_markdown_links_resolve():
    """Documentation links must point at files that exist in the clone."""
    findings = verify_repo.check_markdown_links(ROOT, verify_repo.tracked_files(ROOT))
    failed = [item for item in findings if item.status == "fail"]
    assert not failed, failed[0].detail


def test_replication_entry_points_exist():
    """A new machine needs the bootstrap, doctor, test and transfer scripts."""
    findings = verify_repo.check_required(ROOT)
    failed = [item for item in findings if item.status == "fail"]
    assert not failed, failed[0].detail


def test_scripts_are_executable():
    """Launchers must work without an extra chmod on POSIX."""
    findings = verify_repo.check_executables(ROOT, verify_repo.tracked_files(ROOT))
    failed = [item for item in findings if item.status == "fail"]
    assert not failed, failed[0].detail
