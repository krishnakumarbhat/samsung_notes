"""Reconcile local note PDFs and path-bearing state with the guest inventory.

The guest ``Storage.sqlite`` is only ever read through a ``mode=ro`` URI with
``PRAGMA query_only=ON`` (see ``GUEST_HELPER``); a single guest query is written
to an inventory snapshot so that a passing plan can be applied without asking
the guest twice.  Planning happens before any mutation:

* the compressed and original trees are independent candidate sets for a UUID;
* UUID-less legacy PDFs are mapped through manifest/state paths or a unique
  SHA-256;
* unknown, recycled and replaced-original files are archived (never deleted)
  under ``preserved_originals``, ``unmapped_orphans`` and ``recycled_notes``;
* symlinks are repaired or archived, never dropped;
* only known path columns are updated, each database is backed up first, and
  chunks/FSRS/Elo data is never deleted.

Ambiguous matches, unknown schemas and unresolvable recall path collisions fail
closed with an exact report before anything is mutated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = PROJECT_ROOT / "02_extracted_notes_pdf"
DEFAULT_SAM_ROOT = PROJECT_ROOT

UUID_SUFFIX = re.compile(r"__([0-9a-fA-F-]{36})\.pdf$", re.IGNORECASE)
UUID_ONLY = re.compile(r"^[0-9a-fA-F-]{36}$")
WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
UNSAFE_COMPONENT = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
UNCHANGED = object()

# The guest-side helper is deliberately tiny and uses read-only SQLite only.
# It never opens the database for writing and never touches the WDoc store.
GUEST_HELPER = r'''import json, sqlite3
from pathlib import Path
p = Path.home() / "AppData/Local/Packages/SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState/Storage.sqlite"
c = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
c.execute("PRAGMA query_only=ON")
cats = {r[0]: (r[1], r[2]) for r in c.execute(
    "SELECT UUID,ParentUUID,DisplayName FROM CategoryTreeDB WHERE IsDeleted=0")}
rows = c.execute(
    "SELECT UUID,Title,CategoryUUID,LastModifiedAt,DeletedStatus FROM NoteDB").fetchall()
items = []
for u, t, k, m, d in rows:
    parts, seen = [], set()
    while k in cats and k not in seen:
        seen.add(k); k, n = cats[k]
        if n and n != "Folders":
            parts.append(n)
    items.append(dict(uuid=u, title=t or "Untitled", folder="/".join(reversed(parts)),
                      modified=int(m), deleted_status=int(d or 0)))
c.close()
print(json.dumps(items, ensure_ascii=True))
'''


class ReconcileError(RuntimeError):
    """A fail-closed reconciliation refusal; no mutation may follow."""


class InventoryNote(BaseModel):
    """One validated row of the authoritative guest note inventory."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    uuid: str
    title: str = "Untitled"
    folder: str = ""
    modified: int = 0
    deleted_status: int = 0

    @field_validator("uuid")
    @classmethod
    def _valid_uuid(cls, value: str) -> str:
        if not UUID_ONLY.fullmatch(value):
            raise ValueError(f"invalid note UUID: {value!r}")
        return value

    @field_validator("folder")
    @classmethod
    def _safe_folder(cls, value: str) -> str:
        parts = [safe_component(part) for part in value.split("/") if part]
        return "/".join(parts)


class InventoryMap(dict):
    """Active notes keyed by UUID, also carrying recycled UUIDs and raw rows."""

    def __init__(
        self,
        active: Mapping[str, InventoryNote] | None = None,
        *,
        recycled: Iterable[str] = (),
        raw: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(active or {})
        self.recycled: set[str] = set(recycled)
        self.raw: list[dict[str, Any]] = [dict(row) for row in raw]


def safe_component(value: str) -> str:
    """Sanitize a filesystem component including traversal and duplicate titles."""
    value = UNSAFE_COMPONENT.sub("_", value).strip().strip(".")
    return value[:120] or "Untitled"


def digest(path: Path) -> str:
    """Stream a SHA-256 digest so large PDFs never load into memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def path_key(value: str | Path, base: Path | None = None) -> str:
    """Stable identity for a stored path (relative to ``base`` when needed)."""
    text = str(value)
    if WINDOWS_PATH.match(text):
        return text.replace("\\", "/").lower()
    candidate = Path(text)
    if not candidate.is_absolute() and base is not None:
        candidate = base / candidate
    return str(candidate.resolve())


def identity(path: Path) -> str:
    """Compare regular files by realpath but symlinks by their own location."""
    if path.is_symlink():
        return str(Path(os.path.abspath(path)))
    return str(path.resolve())


def uuid_in(text: str) -> str | None:
    """Return the first lowercase UUID suffix found in a stored path cell."""
    match = UUID_SUFFIX.search(text)
    return match.group(1).lower() if match else None


def fetch_guest_inventory() -> list[dict[str, Any]]:
    """Fetch a fresh inventory using a read-only guest-side SQLite helper."""
    import incremental_export as exporter  # imported lazily; pulls no heavy deps

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        helper = Path(scratch) / "reconcile_inventory.py"
        helper.write_text(GUEST_HELPER, encoding="utf-8")
        remote = "C:/Users/notes/reconcile_inventory.py"
        exporter.upload(helper, remote)
        command = "C:\\py\\python.exe " + remote
        output = exporter.execute(exporter.transport("ssh") + [command], 120)
    line = next((item for item in output.splitlines() if item.startswith("[")), None)
    if line is None:
        raise ReconcileError("guest inventory helper produced no JSON line")
    data = json.loads(line)
    if not isinstance(data, list):
        raise ReconcileError("guest inventory helper returned an unexpected payload")
    return data


def _normalize_inventory(raw: Any) -> InventoryMap:
    """Validate inventory rows, separating active from recycled UUIDs."""
    notes = raw.get("notes") if isinstance(raw, dict) else raw
    if not isinstance(notes, list):
        raise ReconcileError("inventory JSON must be a list or an object with a 'notes' list")
    active: dict[str, InventoryNote] = {}
    recycled: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in notes:
        note = InventoryNote.model_validate(item)
        normalized.append({
            "uuid": note.uuid, "title": note.title, "folder": note.folder,
            "modified": note.modified, "deleted_status": note.deleted_status,
        })
        if note.deleted_status != 0:
            recycled.add(note.uuid)
            continue
        previous = active.get(note.uuid)
        if previous is not None:
            if (previous.folder, previous.title) != (note.folder, note.title):
                raise ReconcileError(f"ambiguous inventory entries for {note.uuid}")
            continue
        active[note.uuid] = note
    if not active:
        raise ReconcileError("inventory contains no active notes; refusing to reconcile")
    return InventoryMap(active, recycled=recycled, raw=normalized)


def _write_snapshot(path: Path, rows: list[dict[str, Any]]) -> None:
    """Persist the single inventory query so ``--apply`` never re-queries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".reconcile-tmp")
    temporary.write_text(
        json.dumps({"notes": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_inventory(path: Path | None, *, snapshot: Path | None = None) -> InventoryMap:
    """Load an offline snapshot or perform exactly one guest inventory query."""
    raw: Any
    if path is not None:
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = fetch_guest_inventory()
    result = _normalize_inventory(raw)
    if snapshot is not None:
        _write_snapshot(snapshot, result.raw)
    return result


class TreeReconciler:
    """Plan and (optionally) apply a full PDF-tree reconciliation."""

    def __init__(
        self,
        root: Path,
        inventory: Mapping[str, InventoryNote],
        *,
        sam_root: Path | None = None,
        archive_id: str | None = None,
        archive_root: Path | None = None,
        recycled: Iterable[str] = (),
    ) -> None:
        """Resolve every path from an explicit root so fixtures stay isolated."""
        self.root = Path(root)
        self.sam_root = Path(sam_root) if sam_root is not None else DEFAULT_SAM_ROOT
        self.inventory = dict(inventory)
        self.recycled = set(recycled)
        self.notes = self.root / "notes"
        self.compressed = self.notes / "compressed"
        self.original = self.notes / "original"
        self.manifest = self.notes / "export_manifest.json"
        self.history = self.root / "ai_agend_history"
        self.state_db = self.history / "incremental_state.sqlite"
        self.index_db = (
            self.sam_root / "04_learning_recall_app/my_notes_graph/data/local_index.sqlite"
        )
        self.recall_db = self.sam_root / "04_learning_recall_app/my_notes_graph/data/recall.sqlite"
        stamp = archive_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.archive_root = Path(archive_root) if archive_root is not None else self.history
        self.backup_root = self.history / "reconcile_backups" / stamp

    # ---------- planning ----------

    def target_for(self, note: InventoryNote) -> Path:
        """Compute the dynamic ``folder/title__<uuid>.pdf`` canonical path in compressed tree."""
        parts = [safe_component(part) for part in note.folder.split("/") if part]
        filename = safe_component(note.title) + "__" + note.uuid + ".pdf"
        return self.compressed.joinpath(*parts, filename)

    def relative(self, path: Path) -> str:
        """Render a path relative to the compressed tree, using POSIX separators."""
        return Path(os.path.relpath(path, self.compressed)).as_posix()

    def check_schemas(self) -> None:
        """Fail closed before planning when any required table/column is absent."""
        required = {
            "incremental_state.sqlite": (self.state_db, {"exports": ("uuid", "path", "derived_path")}),
            "local_index.sqlite": (self.index_db, {"sources": ("source", "path", "status")}),
            "recall.sqlite": (
                self.recall_db,
                {"indexed_files": ("path", "source", "status"), "qa": ("id", "source")},
            ),
        }
        for label, (database, tables) in required.items():
            if not database.is_file():
                raise ReconcileError(f"missing local database for {label}: {database}")
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            try:
                for table, columns in tables.items():
                    present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    missing = [column for column in columns if column not in present]
                    if missing:
                        raise ReconcileError(
                            f"unknown schema in {label}: {table} lacks {', '.join(missing)}"
                        )
            finally:
                connection.close()

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        """Return manifest note records keyed by lowercase UUID."""
        if not self.manifest.is_file():
            return {}
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        entries = payload.get("notes", []) if isinstance(payload, dict) else []
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            uuid = str(entry.get("uuid", "")).lower()
            if UUID_ONLY.fullmatch(uuid):
                result[uuid] = entry
        return result

    def _legacy_index(
        self, manifest: Mapping[str, dict[str, Any]]
    ) -> tuple[dict[str, str], dict[str, set[str]]]:
        """Index manifest and state paths/digests for UUID-less legacy PDFs."""
        by_path: dict[str, str] = {}
        by_sha: dict[str, set[str]] = defaultdict(set)
        for uuid, entry in manifest.items():
            for field in ("path", "derived_path"):
                if entry.get(field):
                    by_path[path_key(str(entry[field]), self.compressed)] = uuid
            for field in ("sha256", "derived_sha256"):
                if entry.get(field):
                    by_sha[str(entry[field]).lower()].add(uuid)
        connection = sqlite3.connect(self.state_db.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(exports)")}
            fields = [field for field in
                      ("path", "derived_path", "sha256", "derived_sha256") if field in columns]
            if "uuid" not in columns or not fields:
                return by_path, by_sha
            statement = "SELECT uuid," + ",".join(fields) + " FROM exports"
            for row in connection.execute(statement):
                uuid = str(row[0]).lower()
                for field, value in zip(fields, row[1:]):
                    if not value:
                        continue
                    if field in ("path", "derived_path"):
                        by_path[path_key(str(value), self.compressed)] = uuid
                    else:
                        by_sha[str(value).lower()].add(uuid)
        finally:
            connection.close()
        return by_path, by_sha

    def _map_legacy(
        self,
        path: Path,
        legacy_index: tuple[dict[str, str], dict[str, set[str]]],
    ) -> str | None:
        """Map a UUID-less PDF by exact stored path, else by a unique SHA-256."""
        by_path, by_sha = legacy_index
        match = by_path.get(path_key(path, self.compressed))
        if match is not None:
            return match
        matches = by_sha.get(digest(path), set())
        return next(iter(matches)) if len(matches) == 1 else None

    def _archive_destination(self, source: Path, reason: str, taken: set[Path]) -> Path:
        """Choose a stable archive path under the category required by ``reason``."""
        if not (source.is_file() or source.is_symlink()):
            raise ReconcileError(f"cannot archive missing source: {source}")
        for base in (self.compressed, self.original, self.root):
            try:
                relative = Path(os.path.relpath(source, base))
            except ValueError:
                continue
            if relative.parts and relative.parts[0] != "..":
                break
        else:
            relative = Path(source.name)
        if reason.startswith("preserved-original"):
            subdir = "preserved_originals"
        elif reason.startswith(("differing-duplicate", "identical-duplicate", "duplicate")):
            subdir = "preserved_originals"
        elif reason.startswith("recycled"):
            subdir = "recycled_notes"
        elif reason.startswith("unmapped"):
            subdir = "unmapped_orphans"
        else:
            subdir = "duplicates"
        candidate = self.archive_root / subdir / relative
        if candidate in taken or candidate.exists() or candidate.is_symlink():
            stem, suffix = candidate.stem, candidate.suffix
            counter = 1
            while candidate in taken or candidate.exists() or candidate.is_symlink():
                candidate = candidate.with_name(f"{stem}~{counter}{suffix}")
                counter += 1
        taken.add(candidate)
        return candidate

    def _archive(
        self,
        source: Path,
        reason: str,
        archives: list[dict[str, str]],
        taken: set[Path],
        *,
        uuid: str | None = None,
    ) -> Path:
        """Record an archive move; the source is never deleted, symlinks included."""
        destination = self._archive_destination(source, reason, taken)
        record: dict[str, str] = {
            "source": str(source),
            "destination": str(destination),
            "reason": reason,
            "sha256": "" if source.is_symlink() else digest(source),
        }
        if uuid:
            record["uuid"] = uuid
        archives.append(record)
        return destination

    def _plan_symlink(
        self,
        link: Path,
        repairs: list[dict[str, str]],
        archives: list[dict[str, str]],
        moves: list[dict[str, str]],
        path_map: dict[str, Path],
        taken: set[Path],
    ) -> None:
        """Archive symlink aliases or broken links without leaving stale paths.

        A canonical compressed PDF is always a standalone file. If a valid PDF
        is linked from an old path, its real file is handled by the PDF group
        planner and the alias is archived. A broken, absolute, non-PDF or
        external link is also archived; it is never rewritten into a link that
        still resolves outside the canonical compressed tree.
        """
        raw = link.readlink()
        match = UUID_SUFFIX.search(link.name) or UUID_SUFFIX.search(str(raw))
        uuid = match.group(1).lower() if match else None
        if self.original in link.parents:
            self._archive(link, "preserved-original-symlink", archives, taken, uuid=uuid)
            return
        if uuid is not None and uuid in self.recycled:
            self._archive(link, "recycled-symlink", archives, taken, uuid=uuid)
            return
        if uuid is not None and uuid not in self.inventory:
            self._archive(link, "unmapped-symlink", archives, taken, uuid=uuid)
            return
        if uuid is None:
            self._archive(link, "unmapped-symlink", archives, taken)
            return
        final = self.target_for(self.inventory[uuid])
        claimed = (
            final.exists()
            or final.is_symlink()
            or final in path_map.values()
            or final in {Path(item["destination"]) for item in moves}
            or final in {Path(item["destination"]) for item in repairs}
        )
        raw_target = raw if raw.is_absolute() else link.parent / raw
        try:
            resolved = raw_target.resolve(strict=True)
        except OSError:
            resolved = None
        safe_target = (
            resolved is not None
            and resolved.is_file()
            and resolved.suffix.lower() == ".pdf"
            and (resolved == final or resolved.is_relative_to(self.compressed.resolve()))
        )
        # A regular candidate for this UUID is preferred and planned above. Any
        # remaining link alias is preserved outside the active tree, avoiding
        # stale folders and broken/absolute symlinks in the canonical store.
        reason = "duplicate-symlink" if claimed or safe_target else "unmapped-symlink"
        self._archive(link, reason, archives, taken, uuid=uuid)

    def _plan_group(
        self, uuid: str, paths: list[Path], manifest: Mapping[str, dict[str, Any]],
        moves: list[dict[str, str]], archives: list[dict[str, str]], taken: set[Path],
    ) -> Path | None:
        """Select one authoritative compressed candidate; archive the others."""
        note = self.inventory[uuid]
        target = self.target_for(note)
        entry = manifest.get(uuid, {})
        expected = entry.get("derived_sha256") or entry.get("sha256")
        manifest_path = entry.get("path")
        candidates = list(paths)
        if target.is_file() and all(identity(path) != identity(target) for path in candidates):
            candidates.append(target)
        if not candidates:
            return None
        digests = {path: digest(path) for path in candidates}
        if expected and not any(value == expected for value in digests.values()):
            raise ReconcileError(f"manifest digest has no on-disk candidate for {uuid}")
        if not expected and len(set(digests.values())) > 1:
            raise ReconcileError(f"ambiguous differing digests without manifest authority for {uuid}")
        valid = [path for path in candidates if not expected or digests[path] == expected]
        preferred: list[Path] = []
        if manifest_path:
            preferred.append(Path(str(manifest_path)))
        preferred.append(target)
        keeper = next(
            (path for path in preferred if any(identity(path) == identity(item) for item in valid)),
            None,
        )
        if keeper is None:
            keeper = sorted(valid, key=lambda item: str(item))[0]
        for path in candidates:
            if identity(path) == identity(keeper):
                continue
            reason = "differing-duplicate" if digests[path] != digests[keeper] else "identical-duplicate"
            self._archive(path, reason, archives, taken, uuid=uuid)
        if identity(keeper) != identity(target):
            if target.exists() or target.is_symlink():
                raise ReconcileError(f"canonical target already exists for {uuid}: {target}")
            moves.append(
                {"uuid": uuid, "source": str(keeper), "destination": str(target),
                 "sha256": digests[keeper], "reason": "canonical-folder-title"}
            )
        return target

    def _cell_value(
        self,
        value: str | None,
        path_map: Mapping[str, Path],
        old_to_new: Mapping[str, Path] | None = None,
    ) -> Any:
        """Return the new value for a path-bearing cell, or UNCHANGED.

        An exact old-path match wins over the UUID fallback so each row follows
        its own file to its own archive instead of colliding at the canonical
        path.
        """
        if not value:
            return UNCHANGED
        text = str(value)
        external = os.path.isabs(text) or bool(WINDOWS_PATH.match(text))
        destination = (old_to_new or {}).get(path_key(text, self.compressed))
        if destination is None:
            uuid = uuid_in(text)
            if uuid is None:
                return UNCHANGED
            destination = path_map.get(uuid)
        if destination is None:
            return UNCHANGED
        return str(destination) if external else self.relative(destination)

    def _collision_error(self, database: str, table: str, column: str, value: str,
                         rows: Sequence[Mapping[str, Any]]) -> ReconcileError:
        """Build an exact, machine-readable fail-closed collision report."""
        return ReconcileError("unresolvable " + database + " collision: " + json.dumps({
            "table": table, "column": column, "value": value,
            "rows": [dict(row) for row in rows],
        }, ensure_ascii=False, sort_keys=True))

    def _resolve_recall_collisions(
        self,
        updates: dict[str, list[dict[str, Any]]],
        resolutions: list[dict[str, Any]],
    ) -> None:
        """Resolve stale same-UUID duplicate paths without deleting learning data."""
        connection = sqlite3.connect(self.recall_db.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            rows = connection.execute(
                "SELECT path,source,chunks,indexed_at,status FROM indexed_files"
            ).fetchall()
        finally:
            connection.close()
        by_path = {row[0]: row for row in rows}
        used = set(by_path)
        moved = {change["path"] for change in updates["recall_files"]}
        inactive = {change["path"] for change in updates["recall_inactive"]}
        occupied = (set(by_path) - moved) | inactive
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for change in updates["recall_files"]:
            grouped[str(change["new_path"])].append(change)

        def stale_path(new_path: str, old_path: str) -> str:
            base = Path(new_path)
            candidate = base.with_name(base.stem + "~stale" + base.suffix)
            counter = 1
            while str(candidate) in used or candidate.exists():
                candidate = base.with_name(f"{base.stem}~stale{counter}{base.suffix}")
                counter += 1
            used.add(str(candidate))
            return str(candidate)

        keep: list[dict[str, Any]] = []
        for new_path, claimants in grouped.items():
            claimant_uuids = {c["uuid"] for c in claimants}
            contenders = list(claimants)
            for existing in sorted(occupied):
                if existing == new_path:
                    continue
                if identity(Path(existing)) == identity(Path(new_path)):
                    row = by_path.get(existing)
                    if row is None:
                        raise self._collision_error(
                            "recall.sqlite", "indexed_files", "path", new_path, claimants
                        )
                    if uuid_in(row[0]) or uuid_in(row[1]):
                        if (uuid_in(row[0]) or uuid_in(row[1])) not in claimant_uuids:
                            raise self._collision_error(
                                "recall.sqlite", "indexed_files", "path", new_path,
                                [*claimants, {"path": row[0], "source": row[1]}],
                            )
                    contenders = [*contenders, {
                        "path": row[0], "source": row[1], "new_path": existing,
                        "uuid": uuid_in(row[0]) or uuid_in(row[1]) or "",
                        "chunks": row[2], "indexed_at": row[3], "exact": True, "occupant": True,
                    }]
            if len(claimant_uuids) > 1:
                raise self._collision_error(
                    "recall.sqlite", "indexed_files", "path", new_path, claimants
                )
            if len(contenders) == 1 and new_path not in occupied:
                keep.append(contenders[0])
                continue
            ordered = sorted(
                contenders,
                key=lambda c: (not bool(c.get("exact")), -int(c.get("chunks") or 0),
                               -float(c.get("indexed_at") or 0), str(c["path"])),
            )
            winner = ordered[0]
            if not winner.get("occupant"):
                keep.append(winner)
            for loser in ordered[1:]:
                if loser.get("occupant"):
                    updates["recall_stale"].append(
                        {"path": loser["path"], "new_path": stale_path(str(loser["new_path"]), loser["path"])}
                    )
                    updates["recall_inactive"].append({"path": loser["path"]})
                    stale = updates["recall_stale"][-1]["new_path"]
                else:
                    stale = stale_path(new_path, loser["path"])
                    updates["recall_stale"].append({"path": loser["path"], "new_path": stale})
                    updates["recall_inactive"].append({"path": loser["path"]})
                resolutions.append({
                    "database": "recall.sqlite", "table": "indexed_files", "column": "path",
                    "contested_path": new_path, "winner": winner["path"],
                    "stale_row": loser["path"], "stale_path": stale,
                    "action": "marked inactive and repathed; chunks/fsrs/elo untouched",
                })
        updates["recall_files"] = keep

    def _plan_updates(
        self, path_map: Mapping[str, Path], old_to_new: Mapping[str, Path],
        resolutions: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Compute concrete row updates for every known path/status column."""
        updates: dict[str, list[dict[str, Any]]] = {
            "state": [], "index_path": [], "index_source": [], "index_inactive": [],
            "recall_files": [], "recall_inactive": [], "recall_stale": [], "recall_qa": [],
        }

        def read(database: Path, sql: str) -> list[tuple[Any, ...]]:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            try:
                return connection.execute(sql).fetchall()
            finally:
                connection.close()

        for uuid, path, derived, title, folder in read(
            self.state_db, "SELECT uuid,path,derived_path,title,folder FROM exports"
        ):
            new_path = self._cell_value(path, path_map, old_to_new)
            new_derived = self._cell_value(derived, path_map, old_to_new)
            note = self.inventory.get(uuid)
            new_title = note.title if note is not None else title
            new_folder = note.folder if note is not None else folder
            path_changed = new_path is not UNCHANGED and new_path != path
            derived_changed = new_derived is not UNCHANGED and new_derived != derived
            if path_changed or derived_changed or new_title != title or new_folder != folder:
                updates["state"].append(
                    {"uuid": uuid, "title": new_title, "folder": new_folder,
                     "path": path if new_path is UNCHANGED else new_path,
                     "derived_path": derived if new_derived is UNCHANGED else new_derived}
                )
        active_source_rows: list[dict[str, Any]] = []
        for identifier, source, path, status in read(
            self.index_db, "SELECT id,source,path,status FROM sources"
        ):
            uuid = uuid_in(str(source)) or uuid_in(str(path))
            if uuid in self.recycled or (uuid is not None and uuid not in self.inventory):
                if status != "inactive":
                    updates["index_inactive"].append({"id": identifier})
                continue
            if uuid:
                active_source_rows.append(
                    {"id": identifier, "uuid": uuid, "source": source, "path": path, "status": status}
                )
            new_path = self._cell_value(path, path_map, old_to_new)
            new_source = self._cell_value(source, path_map, old_to_new)
            note = self.inventory.get(uuid) if uuid else None
            new_folder = note.folder if note is not None else folder
            if new_path is not UNCHANGED and new_path != path:
                updates["index_path"].append({"id": identifier, "path": new_path})
            source_changed = new_source is not UNCHANGED and new_source != source
            if source_changed or new_folder != folder:
                updates["index_source"].append(
                    {"id": identifier,
                     "source": new_source if source_changed else source,
                     "old_source": source,
                     "folder": new_folder}
                )
        # Prune stale duplicate local_index rows: keep one active row per UUID,
        # mark the rest inactive so the active sources count matches live notes.
        by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in active_source_rows:
            by_uuid[row["uuid"]].append(row)
        canonical_rel: dict[str, str | None] = {}
        for uuid, target in path_map.items():
            if not uuid.endswith("_original"):
                canonical_rel[uuid] = self.relative(target)
        for uuid, rows in by_uuid.items():
            if len(rows) <= 1:
                continue
            keep_rel = canonical_rel.get(uuid)
            keeper = None
            for row in rows:
                if keep_rel is not None and row["source"] == keep_rel:
                    keeper = row
                    break
            if keeper is None:
                keeper = rows[0]
            for row in rows:
                if row["id"] != keeper["id"] and row["status"] != "inactive":
                    updates["index_inactive"].append({"id": row["id"]})
        # Drop path/source updates for pruned rows to avoid UNIQUE collisions on source.
        pruned_ids = {
            item["id"]
            for item in updates["index_inactive"]
            if isinstance(item, dict) and "id" in item
        }
        updates["index_path"] = [
            u for u in updates["index_path"] if u.get("id") not in pruned_ids
        ]
        updates["index_source"] = [
            u for u in updates["index_source"] if u.get("id") not in pruned_ids
        ]
        for path, source, status, folder in read(
            self.recall_db, "SELECT path,source,status,folder FROM indexed_files"
        ):
            uuid = uuid_in(str(path)) or uuid_in(str(source))
            if uuid in self.recycled or (uuid is not None and uuid not in self.inventory):
                if status != "inactive":
                    updates["recall_inactive"].append({"path": path})
                continue
            new_path = self._cell_value(path, path_map, old_to_new)
            if new_path is not UNCHANGED and new_path != path:
                new_source = source
                new_folder = folder
                if uuid:
                    note = self.inventory[uuid]
                    new_source = self.relative(Path(new_path))
                    new_folder = note.folder
                updates["recall_files"].append({
                    "path": path, "new_path": new_path,
                    "source": new_source, "old_source": source, "uuid": uuid or "",
                    "folder": new_folder,
                    "exact": path_key(path, self.compressed) in old_to_new,
                    "chunks": 0, "indexed_at": 0.0,
                })
        for identifier, source in read(self.recall_db, "SELECT id,source FROM qa"):
            new_source = self._cell_value(source, path_map, old_to_new)
            if new_source is not UNCHANGED and new_source != source:
                updates["recall_qa"].append({"id": identifier, "source": new_source})
        self._check_collisions(updates, resolutions)
        return updates

    def _check_collisions(
        self, updates: dict[str, list[dict[str, Any]]], resolutions: list[dict[str, Any]]
    ) -> None:
        """Refuse unique-key collisions except resolvable stale recall paths."""
        def values(database: Path, sql: str) -> set[str]:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            try:
                return {row[0] for row in connection.execute(sql)}
            finally:
                connection.close()

        index_sources = values(self.index_db, "SELECT source FROM sources")
        for change in updates["index_source"]:
            index_sources.discard(change["old_source"])
        final_sources: set[str] = set()
        for change in updates["index_source"]:
            value = change["source"]
            if value in final_sources or value in index_sources:
                raise self._collision_error("local_index.sqlite", "sources", "source", value,
                                            updates["index_source"])
            final_sources.add(value)
        self._resolve_recall_collisions(updates, resolutions)

    def build_plan(self) -> dict[str, Any]:
        """Build the complete, validated plan without touching the filesystem."""
        self.check_schemas()
        manifest = self._load_manifest()
        legacy_index = self._legacy_index(manifest)
        groups: dict[str, list[Path]] = defaultdict(list)
        moves: list[dict[str, str]] = []
        archives: list[dict[str, str]] = []
        repairs: list[dict[str, str]] = []
        resolutions: list[dict[str, Any]] = []
        taken: set[Path] = set()
        path_map: dict[str, Path] = {}

        scanned: list[tuple[Path, str]] = []
        symlinks: list[Path] = []
        for base, tree in ((self.compressed, "compressed"), (self.original, "original")):
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_symlink():
                    symlinks.append(path)
                    continue
                if path.is_file() and path.suffix.lower() == ".pdf":
                    scanned.append((path, tree))

        original_groups: dict[str, list[Path]] = defaultdict(list)

        for path, tree in scanned:
            match = UUID_SUFFIX.search(path.name)
            uuid = match.group(1).lower() if match else self._map_legacy(path, legacy_index)
            if uuid is None:
                reason = "preserved-original" if tree == "original" else "unmapped-orphan"
                self._archive(path, reason, archives, taken)
                continue
            if tree == "original":
                # Compressed-only is canonical: archive any physical original
                # under preserved_originals; never populate notes/original/.
                self._archive(path, "preserved-original", archives, taken, uuid=uuid)
                continue
            if uuid in self.inventory:
                groups[uuid].append(path)
            elif uuid in self.recycled:
                self._archive(path, "recycled-deleted", archives, taken, uuid=uuid)
            else:
                self._archive(path, "unmapped-orphan", archives, taken, uuid=uuid)

        for item in archives:
            uuid = item.get("uuid")
            if uuid and uuid not in self.inventory:
                path_map.setdefault(uuid, Path(item["destination"]))
        for uuid in sorted(groups):
            target = self._plan_group(uuid, groups[uuid], manifest, moves, archives, taken)
            if target is not None:
                path_map[uuid] = target
        for uuid in sorted(original_groups):
            target = self._plan_original_group(uuid, original_groups[uuid], manifest, moves, archives, taken)
            if target is not None:
                path_map[uuid + "_original"] = target
        for link in symlinks:
            self._plan_symlink(link, repairs, archives, moves, path_map, taken)
        old_to_new: dict[str, Path] = {}
        for item in archives + moves:
            old_to_new[path_key(item["source"], self.compressed)] = Path(item["destination"])
        updates = self._plan_updates(path_map, old_to_new, resolutions)
        return {
            "inventory_count": len(self.inventory),
            "recycled_count": len(self.recycled),
            "moves": moves,
            "archives": archives,
            "symlink_repairs": repairs,
            "collision_resolutions": resolutions,
            "path_map": {key: str(value) for key, value in path_map.items()},
            "db_updates": {key: len(value) for key, value in updates.items()},
            "_updates": updates,
        }

    # ---------- application ----------

    def _backup(self) -> list[str]:
        """Back up each local database with SQLite's consistent backup API."""
        self.backup_root.mkdir(parents=True, exist_ok=True)
        created: list[str] = []
        for name, database in (
            ("incremental_state.sqlite", self.state_db),
            ("local_index.sqlite", self.index_db),
            ("recall.sqlite", self.recall_db),
        ):
            destination = self.backup_root / name
            source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            source.execute("PRAGMA query_only=ON")
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            created.append(str(destination))
        if self.manifest.is_file():
            copy = self.backup_root / self.manifest.name
            shutil.copy2(self.manifest, copy)
            created.append(str(copy))
        return created

    def _move(self, source: Path, destination: Path) -> None:
        """Move one file or symlink atomically, refusing to overwrite anything."""
        if not (source.is_file() or source.is_symlink()):
            raise ReconcileError(f"missing move source: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise ReconcileError(f"refusing to overwrite existing destination: {destination}")
        os.replace(source, destination)

    def _apply_files(self, plan: Mapping[str, Any]) -> None:
        """Apply archive moves first, then canonical moves, then symlink relocations."""
        for item in plan["archives"]:
            self._move(Path(item["source"]), Path(item["destination"]))
        for item in plan["moves"]:
            self._move(Path(item["source"]), Path(item["destination"]))
        for item in plan["symlink_repairs"]:
            link = Path(item["link"])
            destination = Path(item["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise ReconcileError(f"refusing to overwrite existing destination: {destination}")
            destination.symlink_to(item["new_target"])
            link.unlink()

    def _write_archive_manifest(self, archives: Sequence[Mapping[str, str]]) -> None:
        """Record reversible archive moves in the configured archive root."""
        if not archives:
            return
        self.archive_root.mkdir(parents=True, exist_ok=True)
        manifest_path = self.archive_root / "manifest.json"
        payload: dict[str, Any] = {"archives": []}
        if manifest_path.exists():
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("archives"), list):
                raise ReconcileError(f"invalid archive manifest: {manifest_path}")
        known = {
            (row.get("source"), row.get("destination"))
            for row in payload["archives"] if isinstance(row, dict)
        }
        archived_at = datetime.now(timezone.utc).isoformat()
        for item in archives:
            key = (item.get("source"), item.get("destination"))
            if key not in known:
                payload["archives"].append({**item, "archived_at": archived_at})
                known.add(key)
        temporary = manifest_path.with_name(manifest_path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, manifest_path)

    def _prune(self) -> list[str]:
        """Remove empty directories bottom-up; never remove file contents."""
        pruned: list[str] = []
        for base in (self.original, self.compressed):
            if not base.is_dir():
                continue
            directories = (path for path in base.rglob("*") if path.is_dir())
            for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    continue  # non-empty or asynchronous unlink; contents are preserved
                pruned.append(str(directory))
        return pruned

    def _apply_db(self, updates: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        """Update only path/status columns; never DELETE chunks, FSRS or Elo rows."""
        connections = (
            (self.state_db, updates["state"],
             "UPDATE exports SET path=?, derived_path=?, title=?, folder=? WHERE uuid=?",
             lambda row: (row["path"], row["derived_path"], row["title"], row["folder"], row["uuid"])),
            (self.index_db, updates["index_path"],
             "UPDATE sources SET path=? WHERE id=?",
             lambda row: (row["path"], row["id"])),
            (self.index_db, updates["index_source"],
             "UPDATE sources SET source=?, folder=? WHERE id=?",
             lambda row: (row["source"], row["folder"], row["id"])),
            (self.index_db, updates["index_inactive"],
             "UPDATE sources SET status='inactive' WHERE id=?",
             lambda row: (row["id"],)),
            (self.recall_db, updates["recall_files"],
             "UPDATE indexed_files SET path=?, source=?, folder=? WHERE path=?",
             lambda row: (row["new_path"], row["source"], row["folder"], row["path"])),
            (self.recall_db, updates["recall_inactive"],
             "UPDATE indexed_files SET status='inactive' WHERE path=?",
             lambda row: (row["path"],)),
            (self.recall_db, updates["recall_stale"],
             "UPDATE indexed_files SET path=? WHERE path=?",
             lambda row: (row["new_path"], row["path"])),
            (self.recall_db, updates["recall_qa"],
             "UPDATE qa SET source=? WHERE id=?",
             lambda row: (row["source"], row["id"])),
        )
        for database, rows, statement, params_for in connections:
            if not rows:
                continue
            connection = sqlite3.connect(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for row in rows:
                    connection.execute(statement, params_for(row))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _apply_manifest(self, path_map: Mapping[str, Path]) -> None:
        """Rewrite the manifest to active rows only, atomically."""
        if not self.manifest.is_file():
            return
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        active: list[dict[str, Any]] = []
        for entry in payload.get("notes", []):
            uuid = str(entry.get("uuid", "")).lower()
            if uuid not in self.inventory:
                continue
            destination = path_map.get(uuid)
            if destination is not None:
                for field in ("path", "derived_path"):
                    if field in entry:
                        entry[field] = str(destination)
            note = self.inventory[uuid]
            entry["title"] = note.title
            entry["folder"] = note.folder
            active.append(entry)
        payload["notes"] = active
        temporary = self.manifest.with_name(self.manifest.name + ".reconcile-tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest)

    def _ensure_inventory_directories(self) -> None:
        """Create the active folder hierarchy in notes/compressed only from the inventory.

        Directory names come from the inventory only (no hard-coded names);
        notes with an empty folder stay at the tree root. notes/original/ is
        never created or populated here.
        """
        for note in self.inventory.values():
            if not note.folder:
                continue
            (self.compressed / note.folder).mkdir(parents=True, exist_ok=True)

    def run(self, *, apply: bool) -> dict[str, Any]:
        """Plan the reconciliation and, only when explicitly requested, apply it."""
        plan = self.build_plan()
        if not apply:
            plan["mode"] = "dry-run"
            return plan
        plan["backups"] = self._backup()
        self._write_archive_manifest(plan["archives"])
        self._ensure_inventory_directories()
        self._apply_files(plan)
        plan["pruned_dirs"] = self._prune()
        self._apply_db(plan["_updates"])
        self._apply_manifest(plan["path_map"])
        plan["mode"] = "apply"
        return plan


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; mutation requires an explicit ``--apply`` flag."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="02_extracted_notes_pdf root")
    parser.add_argument("--sam-root", type=Path, default=None, help="workspace root holding local DBs")
    parser.add_argument("--inventory-json", type=Path, default=None, help="offline inventory snapshot")
    parser.add_argument("--inventory-snapshot", type=Path, default=None,
                        help="write the single guest inventory query here for a later --apply")
    parser.add_argument("--archive-root", type=Path, default=None,
                        help="external destination for reversible stale/duplicate file moves")
    parser.add_argument("--apply", action="store_true", help="perform mutations (default: dry-run)")
    parser.add_argument("--report", type=Path, default=None, help="explicit report destination")
    args = parser.parse_args(argv)
    reconciler: TreeReconciler | None = None
    try:
        inventory = load_inventory(args.inventory_json, snapshot=args.inventory_snapshot)
        reconciler = TreeReconciler(
            args.root, inventory, sam_root=args.sam_root, archive_root=args.archive_root,
            recycled=inventory.recycled
        )
        plan = reconciler.run(apply=args.apply)
    except (ReconcileError, ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"reconcile_tree: refused: {exc}", file=sys.stderr)
        return 2
    report = args.report or reconciler.history / (
        "reconcile_tree.apply.json" if args.apply else "reconcile_tree.dryrun.json"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "mode": plan["mode"],
        "inventory": plan["inventory_count"],
        "moves": len(plan["moves"]),
        "archives": len(plan["archives"]),
        "symlink_repairs": len(plan["symlink_repairs"]),
        "collision_resolutions": len(plan["collision_resolutions"]),
        "db_updates": plan["db_updates"],
        "report": str(report),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
