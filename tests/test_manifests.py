"""Validate the committed asset and corpus manifests without the payloads."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "manifests"


def _load(name: str) -> dict:
    path = MANIFESTS / name
    assert path.is_file(), f"missing manifest: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_local_assets_manifest_shape():
    """The public inventory must not disclose private local payload paths."""
    data = _load("local_assets.json")
    assert data["schema"] == 1
    assert "Aggregate counts only" in data["privacy"]
    assert "files" not in data or data["files"] == []
    assert all("root" not in group for group in data["groups"])
    assert all("path" not in group for group in data["groups"])
    assert all("path" in item for item in data.get("reproducible_tool", []))


def test_corpus_manifest_is_aggregate_and_path_free():
    """The public corpus snapshot records counts without revealing note names."""
    data = _load("corpus.json")
    assert data["schema"] == 1
    assert data["totals"]["files"] > 0
    assert data["totals"]["pages"] > 0
    assert data["totals"]["searchable"] + data["totals"]["empty"] <= data["totals"]["files"]
    assert data["files"] == []
    assert "Per-file names omitted" in data["privacy"]


def test_model_manifest_is_pinned():
    """The download script must be able to verify a revision and checksums."""
    data = _load("models.json")
    assert data["repo"] == "BAAI/bge-small-en-v1.5"
    assert len(data["revision"]) >= 7
    names = {item["path"] for item in data["files"]}
    assert "model.safetensors" in names
    assert all(item.get("sha256") for item in data["files"] if item["path"].endswith((".json", ".txt", ".safetensors")))


def test_environment_manifest_records_tested_versions():
    """The environment report captures the interpreter and key package pins."""
    data = _load("environment.json")
    assert data["schema"] == 1
    assert data["python"].startswith("3.")
    packages = {item["name"]: item["version"] for item in data["packages"]}
    for required in ("fastapi", "fsrs", "pypdf", "pytest"):
        assert required in packages, required
        assert packages[required] != "missing"


def test_public_evidence_is_aggregate_and_portable():
    """Published evidence has neither corpus identifiers nor machine paths."""
    summary = json.loads((ROOT / "00_orchestrator/STAGE3_PUBLIC_SUMMARY.json").read_text())
    assert "Aggregate-only" in summary["privacy"]
    assert summary["corpus"]["physical_pdfs"] == summary["corpus"]["indexed_sources"]
    assert "title" not in summary["corpus"]
    for path in (MANIFESTS / "environment.json", MANIFESTS / "corpus.json"):
        text = path.read_text(encoding="utf-8")
        assert "/media/" not in text
        assert "/home/" not in text
