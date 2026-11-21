"""Fixture tests for the fail-closed PDF tree reconciler."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "02_extracted_notes_pdf" / "script"
if str(SCRIPT) not in sys.path:
    sys.path.insert(0, str(SCRIPT))
import reconcile_tree as rt  # noqa: E402

U1 = "c8860b15-10b6-0000-0000-000000000001"
U2 = "c8860b15-10b6-0000-0000-000000000002"
U3 = "c8860b15-10b6-0000-0000-000000000003"


def _pdf(marker: str) -> bytes:
    """Deterministic fake PDF bytes; the reconciler only hashes them."""
    return b"%PDF-1.4\n" + marker.encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _seed_databases(ws: SimpleNamespace, rows: list[dict[str, str]]) -> None:
    """Create real local DBs with the schemas the reconciler requires."""
    with _connect(ws.state_db) as connection:
        connection.execute(
            "CREATE TABLE exports(uuid TEXT PRIMARY KEY, modified INTEGER, title TEXT, folder TEXT, "
            "path TEXT, sha256 TEXT, renderer TEXT, pages INTEGER, ocr INTEGER DEFAULT 0, "
            "chars INTEGER DEFAULT 0, ink_sha256 TEXT, derived_path TEXT, derived_sha256 TEXT, "
            "derived_chars INTEGER, original_sha256 TEXT, derived_renderer TEXT)"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO exports(uuid,path,derived_path,sha256,derived_sha256,title,folder) "
                "VALUES(?,?,?,?,?,?,?)",
                (row["uuid"], row["path"], row["path"], row["sha"], row["sha"], "Old", "old"),
            )
    with _connect(ws.index_db) as connection:
        connection.execute(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY, source TEXT NOT NULL UNIQUE, "
            "path TEXT NOT NULL, folder TEXT NOT NULL, sha256 TEXT NOT NULL, "
            "bytes INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0, "
            "chunks INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL, error TEXT, embed_mode TEXT, indexed_at REAL NOT NULL)"
        )
        for index, row in enumerate(rows, start=1):
            relative = os.path.relpath(row["path"], ws.compressed)
            connection.execute(
                "INSERT INTO sources(id,source,path,folder,sha256,status,indexed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (index, relative, row["path"], "old", row["sha"], "indexed", 1.0),
            )
    with _connect(ws.recall_db) as connection:
        connection.execute(
            "CREATE TABLE indexed_files(path TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "folder TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL, "
            "chunks INTEGER NOT NULL DEFAULT 0, indexed_at REAL NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'indexed')"
        )
        connection.execute(
            "CREATE TABLE qa(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, direction TEXT, "
            "topic TEXT, question TEXT, answer TEXT, correct INTEGER, grade INTEGER, "
            "rubric TEXT NOT NULL DEFAULT '', feedback TEXT NOT NULL DEFAULT '', "
            "mistake_pattern TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT '', "
            "source TEXT NOT NULL DEFAULT '', source_page INTEGER, parent_id INTEGER)"
        )
        connection.execute(
            "CREATE TABLE fsrs_cards(topic TEXT PRIMARY KEY, folder TEXT NOT NULL DEFAULT '', "
            "card_json TEXT NOT NULL, due REAL NOT NULL, stability REAL, difficulty REAL, "
            "last_review REAL, last_rating INTEGER, mistake_pattern TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "CREATE TABLE elo_ratings(scope TEXT NOT NULL, key TEXT NOT NULL, rating REAL NOT NULL, "
            "games INTEGER NOT NULL DEFAULT 0, last_active REAL NOT NULL, PRIMARY KEY(scope,key))"
        )
        for index, row in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO indexed_files(path,source,folder,sha256,indexed_at) VALUES(?,?,?,?,?)",
                (row["path"], Path(row["path"]).name, "old", row["sha"], 1.0),
            )
            connection.execute("INSERT INTO qa(id,source,topic) VALUES(?,?,?)",
                               (index, os.path.relpath(row["path"], ws.compressed), "topic"))
        connection.execute(
            "INSERT INTO fsrs_cards(topic,card_json,due) VALUES('maths','{\"s\":9}',42.0)"
        )
        connection.execute(
            "INSERT INTO elo_ratings(scope,key,rating,games,last_active) "
            "VALUES('learner','maths',1500.0,7,1.0)"
        )


@pytest.fixture
def ws(tmp_path: Path) -> SimpleNamespace:
    """A complete miniature corpus with three local DBs and a manifest."""
    root = tmp_path / "02_extracted_notes_pdf"
    notes = root / "notes"
    compressed = notes / "compressed"
    original = notes / "original"
    history = root / "ai_agend_history"
    for directory in (compressed, original, history):
        directory.mkdir(parents=True, exist_ok=True)
    ws = SimpleNamespace(
        tmp=tmp_path, root=root, notes=notes, compressed=compressed, original=original,
        history=history, manifest=notes / "export_manifest.json",
        state_db=history / "incremental_state.sqlite",
        index_db=tmp_path / "04_learning_recall_app/my_notes_graph/data/local_index.sqlite",
        recall_db=tmp_path / "04_learning_recall_app/my_notes_graph/data/recall.sqlite",
    )
    ws.index_db.parent.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def inventory() -> dict[str, rt.InventoryNote]:
    return {
        U1: rt.InventoryNote(uuid=U1, title="Graph Analysis", folder="maths/04. Analysis", modified=1),
        U3: rt.InventoryNote(uuid=U3, title="Notes", folder="Dup", modified=3),
    }


def _reconciler(ws: SimpleNamespace, inventory: dict[str, rt.InventoryNote]) -> rt.TreeReconciler:
    return rt.TreeReconciler(ws.root, inventory, sam_root=ws.tmp, archive_id="fixture")


def _stale_pdf(ws: SimpleNamespace, uuid: str, marker: str) -> Path:
    path = ws.compressed / "old_folder" / f"OldName__{uuid}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pdf(marker))
    return path


def _write_manifest(ws: SimpleNamespace, entries: list[dict[str, str]]) -> None:
    ws.manifest.write_text(json.dumps({"generated_at": "now", "notes": entries}, indent=2))


def _write_inventory_json(ws: SimpleNamespace, notes: list[dict[str, object]]) -> Path:
    path = ws.tmp / "inventory.json"
    path.write_text(json.dumps({"db_size_bytes": 1, "notes": notes}))
    return path


def _state_paths(ws: SimpleNamespace) -> list[tuple[str, str]]:
    with _connect(ws.state_db) as connection:
        return connection.execute("SELECT path,derived_path FROM exports").fetchall()


def _learning_rows(ws: SimpleNamespace) -> tuple[object, object]:
    with _connect(ws.recall_db) as connection:
        fsrs = connection.execute("SELECT * FROM fsrs_cards").fetchall()
        elo = connection.execute("SELECT * FROM elo_ratings").fetchall()
    return fsrs, elo


def _db_snapshot(db: Path, table: str) -> tuple:
    with _connect(db) as connection:
        return tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())


def test_guest_helper_never_opens_storage_for_writing() -> None:
    assert "p.as_uri() + \"?mode=ro\", uri=True" in rt.GUEST_HELPER
    assert "PRAGMA query_only=ON" in rt.GUEST_HELPER
    assert "UPDATE " not in rt.GUEST_HELPER.upper()
    assert "DELETE FROM" not in rt.GUEST_HELPER.upper()


def test_offline_inventory_normalizes_and_rejects_conflicts(ws: SimpleNamespace) -> None:
    path = _write_inventory_json(ws, [
        {"uuid": U1, "title": "OK", "folder": "maths", "deleted_status": 0},
        {"uuid": U2, "title": "Recycled", "folder": "old", "deleted_status": 1},
    ])
    loaded = rt.load_inventory(path)
    assert set(loaded) == {U1}
    assert loaded[U1].folder == "maths"
    conflicting = _write_inventory_json(ws, [
        {"uuid": U1, "folder": "a", "title": "A"},
        {"uuid": U1, "folder": "b", "title": "B"},
    ])
    with pytest.raises(rt.ReconcileError, match="ambiguous inventory"):
        rt.load_inventory(conflicting)


def test_dry_run_reports_move_without_mutating(ws: SimpleNamespace, inventory: dict) -> None:
    old = _stale_pdf(ws, U1, "v1")
    digest = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": digest}])
    _write_manifest(ws, [{"uuid": U1, "path": str(old), "sha256": digest, "derived_sha256": digest}])
    plan = _reconciler(ws, inventory).run(apply=False)
    assert plan["mode"] == "dry-run"
    assert len(plan["moves"]) == 1
    assert plan["moves"][0]["uuid"] == U1
    assert old.is_file()
    assert _state_paths(ws) == [(str(old), str(old))]
    assert not (ws.history / "reconcile_backups").exists()


def test_apply_moves_to_dynamic_path_and_updates_all_known_columns(
    ws: SimpleNamespace, inventory: dict
) -> None:
    old = _stale_pdf(ws, U1, "v1")
    digest = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": digest}])
    _write_manifest(ws, [{"uuid": U1, "path": str(old), "sha256": digest, "derived_sha256": digest}])
    before_fsrs, before_elo = _learning_rows(ws)
    target = ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf"
    plan = _reconciler(ws, inventory).run(apply=True)
    assert plan["mode"] == "apply"
    assert target.is_file() and not old.exists()
    assert not (ws.compressed / "old_folder").exists()  # empty dir pruned
    assert _state_paths(ws) == [(str(target), str(target))]
    manifest = json.loads(ws.manifest.read_text())
    assert manifest["notes"][0]["path"] == str(target)
    with _connect(ws.index_db) as connection:
        assert connection.execute("SELECT source,path FROM sources").fetchone() == (
            f"maths/04. Analysis/Graph Analysis__{U1}.pdf", str(target)
        )
    with _connect(ws.recall_db) as connection:
        assert connection.execute("SELECT path FROM indexed_files").fetchone()[0] == str(target)
        assert connection.execute("SELECT source FROM qa").fetchone()[0] == (
            f"maths/04. Analysis/Graph Analysis__{U1}.pdf"
        )
    assert _learning_rows(ws) == (before_fsrs, before_elo)
    assert (ws.history / "reconcile_backups/fixture/incremental_state.sqlite").is_file()
    assert (ws.history / "reconcile_backups/fixture/local_index.sqlite").is_file()
    assert (ws.history / "reconcile_backups/fixture/recall.sqlite").is_file()
    assert (ws.history / "reconcile_backups/fixture/export_manifest.json").is_file()


def test_archives_differing_duplicate_and_unmapped_without_deletion(
    ws: SimpleNamespace, inventory: dict
) -> None:
    keeper = ws.compressed / "Dup" / f"Notes__{U3}.pdf"
    keeper.parent.mkdir()
    keeper.write_bytes(_pdf("keeper"))
    divergent = ws.compressed / "Other" / f"Notes__{U3}.pdf"
    divergent.parent.mkdir()
    divergent.write_bytes(_pdf("divergent"))
    recycled = ws.compressed / "Recycled" / f"Old__{U2}.pdf"
    recycled.parent.mkdir()
    recycled.write_bytes(_pdf("recycled"))
    digest = _sha(_pdf("keeper"))
    _seed_databases(ws, [])
    _write_manifest(ws, [{"uuid": U3, "path": str(keeper), "sha256": digest, "derived_sha256": digest}])
    plan = _reconciler(ws, inventory).run(apply=True)
    reasons = {item["reason"] for item in plan["archives"]}
    assert "differing-duplicate" in reasons
    assert "unmapped-orphan" in reasons
    archived = [Path(item["destination"]) for item in plan["archives"]]
    assert len(archived) == 2
    assert all(path.is_file() for path in archived)  # never deleted
    assert any("unmapped_orphans" in str(path) for path in archived)
    assert any("preserved_originals" in str(path) for path in archived)
    assert keeper.is_file() and not divergent.exists() and not recycled.exists()


def test_duplicate_db_rows_follow_each_file_to_distinct_paths(
    ws: SimpleNamespace, inventory: dict
) -> None:
    keeper = ws.compressed / "Dup" / f"Notes__{U3}.pdf"
    keeper.parent.mkdir()
    keeper.write_bytes(_pdf("keeper"))
    divergent = ws.compressed / "Other" / f"Notes__{U3}.pdf"
    divergent.parent.mkdir()
    divergent.write_bytes(_pdf("divergent"))
    digest = _sha(_pdf("keeper"))
    _seed_databases(ws, [
        {"uuid": U3, "path": str(divergent), "sha": _sha(_pdf("divergent"))},
    ])
    with _connect(ws.index_db) as connection:
        connection.execute(
            "INSERT INTO sources(id,source,path,folder,sha256,status,indexed_at) "
            "VALUES(2,?,?,?,?,?,?)",
            (os.path.relpath(keeper, ws.compressed), str(keeper), "Dup", digest, "indexed", 1.0),
        )
    _write_manifest(ws, [{"uuid": U3, "path": str(keeper), "sha256": digest, "derived_sha256": digest}])
    plan = _reconciler(ws, inventory).run(apply=True)
    with _connect(ws.index_db) as connection:
        sources = {row[0] for row in connection.execute("SELECT source FROM sources")}
        active = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE status='indexed'"
        ).fetchone()[0]
        inactive = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE status='inactive'"
        ).fetchone()[0]
    assert len(sources) == 2  # no UNIQUE collision; each row followed its own file
    assert any("Dup/Notes__" in value for value in sources)
    # One active (keeper) and one inactive (divergent, archived as duplicate)
    assert active == 1
    assert inactive == 1
    assert len(plan["archives"]) == 1
    assert plan["archives"][0]["reason"] == "differing-duplicate"


def test_ambiguous_differing_digests_fail_closed(ws: SimpleNamespace, inventory: dict) -> None:
    first = ws.compressed / "A" / f"Notes__{U3}.pdf"
    first.parent.mkdir()
    first.write_bytes(_pdf("one"))
    second = ws.compressed / "B" / f"Notes__{U3}.pdf"
    second.parent.mkdir()
    second.write_bytes(_pdf("two"))
    _seed_databases(ws, [])
    _write_manifest(ws, [])
    with pytest.raises(rt.ReconcileError, match="ambiguous differing digests"):
        _reconciler(ws, inventory).run(apply=False)
    assert first.is_file() and second.is_file()


def test_unknown_schema_fails_closed_before_mutation(ws: SimpleNamespace, inventory: dict) -> None:
    old = _stale_pdf(ws, U1, "v1")
    digest = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": digest}])
    with _connect(ws.recall_db) as connection:
        connection.execute("ALTER TABLE qa RENAME TO qa_legacy")
    with pytest.raises(rt.ReconcileError, match="unknown schema"):
        _reconciler(ws, inventory).run(apply=False)
    assert old.is_file()


def test_repairs_relative_symlink_after_move(ws: SimpleNamespace, inventory: dict) -> None:
    old = _stale_pdf(ws, U1, "v1")
    digest = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": digest}])
    _write_manifest(ws, [{"uuid": U1, "path": str(old), "sha256": digest, "derived_sha256": digest}])
    links = ws.compressed / "links"
    links.mkdir()
    link = links / f"Pointer__{U1}.pdf"
    link.symlink_to(os.path.relpath(old, links))
    plan = _reconciler(ws, inventory).run(apply=True)
    canonical = ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf"
    assert canonical.is_file() and not old.exists()
    # The canonical file owns the canonical path, so the alias is archived,
    # never left in place, and the stale link directory is pruned.
    assert not links.exists()
    assert not any(r["uuid"] == U1 for r in plan["symlink_repairs"])
    # notes/original must stay absent/empty; compressed-only folder creation.
    assert not any(ws.original.rglob("*"))
    archived = [a for a in plan["archives"] if a.get("uuid") == U1
                and a["reason"] == "duplicate-symlink"]
    assert archived, "expected the alias symlink to be archived"
    destination = Path(archived[0]["destination"])
    assert "preserved_originals" in str(destination)
    assert destination.is_symlink()
    assert os.readlink(destination) == os.path.relpath(old, links)


def test_cli_defaults_to_dry_run_and_requires_explicit_apply(
    ws: SimpleNamespace, inventory: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _stale_pdf(ws, U1, "v1")
    digest = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": digest}])
    _write_manifest(ws, [{"uuid": U1, "path": str(old), "sha256": digest, "derived_sha256": digest}])
    fixture = _write_inventory_json(ws, [
        {"uuid": U1, "title": "Graph Analysis", "folder": "maths/04. Analysis", "modified": 1},
    ])
    report = ws.history / "cli-report.json"
    base = ["--root", str(ws.root), "--sam-root", str(ws.tmp),
            "--inventory-json", str(fixture), "--report", str(report)]
    assert rt.main(base) == 0
    assert json.loads(report.read_text())["mode"] == "dry-run"
    assert old.is_file()
    assert "dry-run" in capsys.readouterr().out
    assert rt.main(base + ["--apply"]) == 0
    assert json.loads(report.read_text())["mode"] == "apply"
    assert (ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf").is_file()


# --------------------------------------------------------------------------- #
# Regression tests for the reconciliation mission
# --------------------------------------------------------------------------- #

def _reconciler_with_recycled(
    ws: SimpleNamespace, inventory: dict[str, rt.InventoryNote], recycled: set[str]
) -> rt.TreeReconciler:
    return rt.TreeReconciler(
        ws.root, inventory, sam_root=ws.tmp, archive_id="fixture", recycled=recycled
    )


def test_original_and_compressed_candidates_for_one_uuid_are_separate(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """A raw original must never be treated as a competing compressed duplicate.

    Compressed-only is canonical: the original PDF is archived under
    preserved_originals (not copied), and only the compressed tree is populated.
    """
    original = ws.original / "raw" / f"Raw__{U1}.pdf"
    original.parent.mkdir()
    original.write_bytes(_pdf("raw"))
    stale = ws.compressed / "old" / f"OldName__{U1}.pdf"
    stale.parent.mkdir()
    stale.write_bytes(_pdf("v1"))
    good = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(stale), "sha": good}])
    _write_manifest(ws, [{"uuid": U1, "path": str(stale), "sha256": good, "derived_sha256": good}])
    plan = _reconciler(ws, inventory).run(apply=True)
    # Only one move: compressed to canonical; original is archived, not moved
    assert len(plan["moves"]) == 1
    assert plan["moves"][0]["uuid"] == U1
    assert plan["moves"][0]["reason"] == "canonical-folder-title"
    assert "/compressed/" in plan["moves"][0]["source"]
    assert "/original/" not in plan["moves"][0]["source"]
    # Compressed canonical exists
    assert (ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf").is_file()
    # Original tree remains empty (no population)
    assert not any(ws.original.rglob("*.pdf"))
    # Original archived under preserved_originals, content preserved
    archived = next(a for a in plan["archives"] if a.get("uuid") == U1)
    assert archived["reason"] == "preserved-original"
    assert "preserved_originals" in archived["destination"]
    assert Path(archived["destination"]).is_file()
    assert Path(archived["destination"]).read_bytes() == _pdf("raw")
    # Original source no longer exists (moved to archive)
    assert not original.exists()


def test_uuid_compressed_only_canonical_symlink_resolves(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """Compressed-only is canonical: original archived, symlink resolves in compressed tree."""
    # Compressed stale PDF
    stale = ws.compressed / "old" / f"OldName__{U1}.pdf"
    stale.parent.mkdir()
    stale.write_bytes(_pdf("v1"))
    # Original raw PDF (should be archived, not copied to canonical)
    original = ws.original / "raw" / f"Raw__{U1}.pdf"
    original.parent.mkdir()
    original.write_bytes(_pdf("raw"))
    good = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(stale), "sha": good}])
    _write_manifest(ws, [{"uuid": U1, "path": str(stale), "sha256": good, "derived_sha256": good}])
    # Relative symlink in compressed tree pointing to the stale compressed PDF
    links = ws.compressed / "links"
    links.mkdir()
    link = links / f"Pointer__{U1}.pdf"
    link.symlink_to(os.path.relpath(stale, links))
    plan = _reconciler(ws, inventory).run(apply=True)
    # Compressed canonical exists
    compressed_canon = ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf"
    assert compressed_canon.is_file()
    # Original tree remains empty (compressed-only canonical)
    assert not any(ws.original.rglob("*.pdf"))
    # Original archived under preserved_originals
    archived = next(a for a in plan["archives"] if a.get("uuid") == U1)
    assert archived["reason"] == "preserved-original"
    assert "preserved_originals" in archived["destination"]
    assert Path(archived["destination"]).is_file()
    assert Path(archived["destination"]).read_bytes() == _pdf("raw")
    assert not original.exists()
    # The canonical file owns the canonical path, so the alias is archived and
    # the stale link directory is pruned; the symlink is never repaired in place.
    assert not links.exists()
    assert not any(r["uuid"] == U1 for r in plan["symlink_repairs"])
    archived = [a for a in plan["archives"] if a.get("uuid") == U1
                and a["reason"] == "duplicate-symlink"]
    assert archived
    assert Path(archived[0]["destination"]).is_symlink()


def test_active_symlink_to_external_pdf_is_archived_for_safe_reexport(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """An external link is not promoted into the canonical compressed store."""
    external = ws.tmp / "external" / f"Real__{U1}.pdf"
    external.parent.mkdir()
    external.write_bytes(_pdf("real"))
    digest = _sha(_pdf("real"))
    links = ws.compressed / "links"
    links.mkdir()
    link = links / f"Pointer__{U1}.pdf"
    link.symlink_to(os.path.relpath(external, links))
    _seed_databases(ws, [{"uuid": U1, "path": str(external), "sha": digest}])
    _write_manifest(ws, [{"uuid": U1, "path": str(external), "sha256": digest,
                          "derived_sha256": digest}])
    plan = _reconciler(ws, inventory).run(apply=True)
    canonical = ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf"
    assert not canonical.exists()
    assert not links.exists()  # stale alias directory pruned
    assert external.is_file()  # preserve the outside source for a supervised export
    archived = next(a for a in plan["archives"] if a.get("uuid") == U1)
    assert archived["reason"] == "unmapped-symlink"
    assert "unmapped_orphans" in archived["destination"]
    assert Path(archived["destination"]).is_symlink()
    assert not any(ws.original.rglob("*"))


def test_metadata_updates_synced_to_inventory_and_idempotent(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """exports.title/folder, manifest title/folder/path, local_index.sources.folder
    and recall.indexed_files.source/folder are synced to the inventory; a second
    run changes nothing (a true no-op, no reindexing)."""
    old = _stale_pdf(ws, U1, "v1")
    digest = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": digest}])
    _write_manifest(ws, [{"uuid": U1, "path": str(old), "sha256": digest,
                          "derived_sha256": digest}])
    plan = _reconciler(ws, inventory).run(apply=True)
    canonical = ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf"
    assert canonical.is_file()
    assert not any(ws.original.rglob("*"))  # notes/original stays absent/empty
    with _connect(ws.state_db) as connection:
        row = connection.execute(
            "SELECT title, folder, path, derived_path FROM exports WHERE uuid=?", (U1,)
        ).fetchone()
    assert row == ("Graph Analysis", "maths/04. Analysis", str(canonical), str(canonical))
    manifest = json.loads(ws.manifest.read_text())["notes"][0]
    assert manifest["title"] == "Graph Analysis"
    assert manifest["folder"] == "maths/04. Analysis"
    assert manifest["path"] == str(canonical)
    with _connect(ws.index_db) as connection:
        source, path, folder = connection.execute(
            "SELECT source, path, folder FROM sources WHERE id=1"
        ).fetchone()
    assert path == str(canonical)
    assert folder == "maths/04. Analysis"
    with _connect(ws.recall_db) as connection:
        rpath, rsource, rfolder = connection.execute(
            "SELECT path, source, folder FROM indexed_files"
        ).fetchone()
    assert rpath == str(canonical)
    assert rfolder == "maths/04. Analysis"
    before = {
        "state": _db_snapshot(ws.state_db, "exports"),
        "index": _db_snapshot(ws.index_db, "sources"),
        "recall": _db_snapshot(ws.recall_db, "indexed_files"),
    }
    second = _reconciler(ws, inventory).run(apply=True)
    assert second["db_updates"] == {
        "state": 0, "index_path": 0, "index_source": 0, "index_inactive": 0,
        "recall_files": 0, "recall_inactive": 0, "recall_stale": 0, "recall_qa": 0,
    }
    after = {
        "state": _db_snapshot(ws.state_db, "exports"),
        "index": _db_snapshot(ws.index_db, "sources"),
        "recall": _db_snapshot(ws.recall_db, "indexed_files"),
    }
    assert after == before


def test_legacy_pdf_mapped_by_manifest_path(ws: SimpleNamespace, inventory: dict) -> None:
    """A UUID-less legacy PDF is mapped through the manifest path."""
    legacy = ws.compressed / "legacy" / "Legacy.pdf"
    legacy.parent.mkdir()
    legacy.write_bytes(_pdf("v1"))
    good = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(legacy), "sha": good}])
    _write_manifest(ws, [{"uuid": U1, "path": str(legacy), "sha256": good, "derived_sha256": good}])
    plan = _reconciler(ws, inventory).run(apply=True)
    assert len(plan["moves"]) == 1
    assert plan["moves"][0]["source"] == str(legacy)
    assert (ws.compressed / "maths/04. Analysis" / f"Graph Analysis__{U1}.pdf").is_file()


def test_legacy_pdf_mapped_by_unique_sha256(ws: SimpleNamespace, inventory: dict) -> None:
    """When no stored path matches, a unique SHA-256 determines the UUID."""
    legacy = ws.compressed / "legacy" / "NoName.pdf"
    legacy.parent.mkdir()
    legacy.write_bytes(_pdf("v1"))
    good = _sha(_pdf("v1"))
    unrelated = ws.compressed / "old" / f"OldName__{U1}.pdf"
    _seed_databases(ws, [])
    _write_manifest(ws, [{"uuid": U1, "path": str(unrelated), "sha256": good,
                          "derived_sha256": good}])
    plan = _reconciler(ws, inventory).run(apply=False)
    assert len(plan["moves"]) == 1 and plan["moves"][0]["source"] == str(legacy)
    assert legacy.is_file()  # dry run never mutates


def test_legacy_pdf_with_non_unique_sha_becomes_orphan(
    ws: SimpleNamespace, inventory: dict
) -> None:
    legacy = ws.compressed / "legacy" / "Ambiguous.pdf"
    legacy.parent.mkdir()
    legacy.write_bytes(_pdf("v1"))
    good = _sha(_pdf("v1"))
    _seed_databases(ws, [])
    _write_manifest(ws, [
        {"uuid": U1, "sha256": good}, {"uuid": U3, "sha256": good},
    ])
    plan = _reconciler(ws, inventory).run(apply=True)
    assert [a["reason"] for a in plan["archives"]] == ["unmapped-orphan"]
    assert "unmapped_orphans" in plan["archives"][0]["destination"]
    assert Path(plan["archives"][0]["destination"]).is_file()


def test_recycled_note_archived_and_index_marked_inactive(
    ws: SimpleNamespace,
) -> None:
    """Deleted notes go to recycled_notes; learning rows are never deleted."""
    inventory = rt.load_inventory(_write_inventory_json(ws, [
        {"uuid": U1, "title": "Graph Analysis", "folder": "maths/04. Analysis", "modified": 1},
        {"uuid": U2, "title": "Recycled", "folder": "old", "modified": 2, "deleted_status": 1},
    ]))
    assert inventory.recycled == {U2}
    recycled = ws.compressed / "Recycled" / f"Old__{U2}.pdf"
    recycled.parent.mkdir()
    recycled.write_bytes(_pdf("gone"))
    good = _sha(_pdf("gone"))
    _seed_databases(ws, [{"uuid": U2, "path": str(recycled), "sha": good}])
    before_fsrs, before_elo = _learning_rows(ws)
    reconciler = _reconciler_with_recycled(ws, inventory, inventory.recycled)
    plan = reconciler.run(apply=True)
    archive = next(a for a in plan["archives"] if a["uuid"] == U2)
    assert "recycled_notes" in archive["destination"] and "recycled-deleted" == archive["reason"]
    assert Path(archive["destination"]).is_file()
    with _connect(ws.index_db) as connection:
        assert connection.execute("SELECT status FROM sources").fetchall() == [("inactive",)]
    with _connect(ws.recall_db) as connection:
        assert connection.execute("SELECT status FROM indexed_files").fetchall() == [("inactive",)]
    assert _learning_rows(ws) == (before_fsrs, before_elo)


def test_local_index_duplicate_rows_pruned_to_one_active_per_uuid(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """Duplicate local_index sources rows for the same UUID are pruned to one active."""
    stale = ws.compressed / "old" / f"OldName__{U1}.pdf"
    stale.parent.mkdir()
    stale.write_bytes(_pdf("v1"))
    good = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(stale), "sha": good}])
    # Add a duplicate sources row for the same UUID (pre-rename stale row)
    with _connect(ws.index_db) as connection:
        connection.execute(
            "INSERT INTO sources(id,source,path,folder,sha256,status,indexed_at) "
            "VALUES(2,?,?,?,?,?,?)",
            (os.path.relpath(stale, ws.compressed) + ".dup", str(stale), "old", good, "indexed", 1.0),
        )
    _write_manifest(ws, [{"uuid": U1, "path": str(stale), "sha256": good, "derived_sha256": good}])
    before_fsrs, before_elo = _learning_rows(ws)
    plan = _reconciler(ws, inventory).run(apply=True)
    with _connect(ws.index_db) as connection:
        active = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE status='indexed'"
        ).fetchone()[0]
        inactive = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE status='inactive'"
        ).fetchone()[0]
    # One active source per UUID; the duplicate is pruned to inactive
    assert active == 1
    assert inactive == 1
    # FSRS/Elo learning data preserved
    assert _learning_rows(ws) == (before_fsrs, before_elo)


def test_unmapped_symlink_is_archived_not_dropped(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """Unknown symlinks are moved to unmapped_orphans, still symlinks."""
    links = ws.compressed / "links"
    links.mkdir()
    link = links / f"Lost__{U2}.pdf"
    link.symlink_to("../../missing.pdf")
    _seed_databases(ws, [])
    plan = _reconciler(ws, inventory).run(apply=True)
    item = next(a for a in plan["archives"] if a["reason"] == "unmapped-symlink")
    assert "unmapped_orphans" in item["destination"] and item["sha256"] == ""
    assert not link.is_symlink()
    archived = Path(item["destination"])
    assert archived.is_symlink() and os.readlink(archived) == "../../missing.pdf"


def test_manifest_rewritten_to_active_rows_only(ws: SimpleNamespace, inventory: dict) -> None:
    old = _stale_pdf(ws, U1, "v1")
    good = _sha(_pdf("v1"))
    _seed_databases(ws, [{"uuid": U1, "path": str(old), "sha": good}])
    _write_manifest(ws, [
        {"uuid": U1, "path": str(old), "sha256": good},
        {"uuid": U2, "path": str(old), "sha256": good},
    ])
    _reconciler(ws, inventory).run(apply=True)
    notes = json.loads(ws.manifest.read_text())["notes"]
    assert [entry["uuid"] for entry in notes] == [U1]
    backup = ws.history / "reconcile_backups/fixture/export_manifest.json"
    assert len(json.loads(backup.read_text())["notes"]) == 2


def test_recall_duplicate_paths_resolved_without_deleting_learning_data(
    ws: SimpleNamespace, inventory: dict
) -> None:
    """Stale same-UUID recall paths are de-duplicated, never deleted."""
    keeper = ws.compressed / "Dup" / f"Notes__{U3}.pdf"
    keeper.parent.mkdir()
    keeper.write_bytes(_pdf("keeper"))
    good = _sha(_pdf("keeper"))
    _seed_databases(ws, [{"uuid": U1, "path": str(_stale_pdf(ws, U1, "u1")),
                          "sha": _sha(_pdf("u1"))}])
    with _connect(ws.recall_db) as connection:
        for path, chunks in (("stale1/Notes__" + U3 + ".pdf", 5),
                             ("stale2/Notes__" + U3 + ".pdf", 2)):
            connection.execute(
                "INSERT INTO indexed_files(path,source,folder,sha256,chunks,indexed_at) "
                "VALUES(?,?,?,?,?,?)",
                (path, Path(path).name, "old", good, chunks, 1.0),
            )
    _write_manifest(ws, [{"uuid": U3, "path": str(keeper), "sha256": good}])
    before_fsrs, before_elo = _learning_rows(ws)
    plan = _reconciler(ws, inventory).run(apply=True)
    assert plan["collision_resolutions"], "expected a stale-path resolution"
    with _connect(ws.recall_db) as connection:
        rows = connection.execute(
            "SELECT path,status FROM indexed_files WHERE source LIKE ?", (f"%{U3}%",)
        ).fetchall()
    assert len(rows) == 2  # neither row deleted
    statuses = dict(rows)
    assert statuses["Dup/Notes__" + U3 + ".pdf"] == "indexed"
    assert any("~stale" in path and status == "inactive" for path, status in rows)
    assert _learning_rows(ws) == (before_fsrs, before_elo)


def test_unresolvable_recall_collision_fails_closed_with_exact_report(
    ws: SimpleNamespace, inventory: dict
) -> None:
    _seed_databases(ws, [])
    with _connect(ws.recall_db) as connection:
        connection.execute(
            "INSERT INTO indexed_files(path,source,sha256,chunks,indexed_at) "
            "VALUES('a.pdf','a',?,0,1.0)", (_sha(_pdf("a")),)
        )
        connection.execute(
            "INSERT INTO indexed_files(path,source,sha256,chunks,indexed_at) "
            "VALUES('b.pdf','b',?,0,1.0)", (_sha(_pdf("b")),)
        )
    reconciler = _reconciler(ws, inventory)
    updates: dict[str, list[dict[str, object]]] = {
        "state": [], "index_path": [], "index_source": [], "index_inactive": [],
        "recall_files": [
            {"path": "a.pdf", "new_path": "shared.pdf", "source": "a", "uuid": U1,
             "exact": True, "chunks": 0, "indexed_at": 0.0},
            {"path": "b.pdf", "new_path": "shared.pdf", "source": "b", "uuid": U3,
             "exact": True, "chunks": 0, "indexed_at": 0.0},
        ],
        "recall_inactive": [], "recall_stale": [], "recall_qa": [],
    }
    with pytest.raises(rt.ReconcileError) as error:
        reconciler._check_collisions(updates, [])
    text = str(error.value)
    assert "recall.sqlite" in text and "shared.pdf" in text
    assert "a.pdf" in text and "b.pdf" in text  # exact rows in the report


def test_apply_db_never_issues_delete_statements() -> None:
    import inspect
    source = inspect.getsource(rt.TreeReconciler._apply_db)
    assert "DELETE FROM" not in source.upper()
    assert all(word in source for word in ("UPDATE sources", "UPDATE indexed_files", "UPDATE qa"))


def test_single_guest_inventory_query_via_snapshot(
    ws: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    def fake_fetch() -> list[dict[str, object]]:
        calls["count"] += 1
        return [{"uuid": U1, "title": "Graph Analysis", "folder": "maths", "modified": 1}]

    monkeypatch.setattr(rt, "fetch_guest_inventory", fake_fetch)
    snapshot = ws.history / "reconcile_inventory.json"
    first = rt.load_inventory(None, snapshot=snapshot)
    second = rt.load_inventory(snapshot)
    assert calls["count"] == 1
    assert set(first) == set(second) == {U1}
    assert json.loads(snapshot.read_text())["notes"][0]["uuid"] == U1


def test_sync_wiring_applies_only_a_passing_single_query_plan() -> None:
    script = (Path(__file__).resolve().parents[1] / "00_orchestrator"
              / "sync_and_export_notes.sh").read_text()
    assert "--inventory-snapshot" in script
    assert "--apply --inventory-json" in script
    assert "RECONCILE_STATUS" in script and "RECONCILE_APPLY_STATUS" in script
    assert script.index("--inventory-snapshot") < script.index("--apply --inventory-json")
