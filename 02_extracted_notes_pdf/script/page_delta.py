"""Page-level content-addressed cache renderer for Samsung Notes exports.

This module is a drop-in replacement for ``incremental_export.render``:

    original_pdf, pages, warnings = compile_note(source, work)

Unlike the full-document renderer it renders one visible page at a time and
reuses cached single-page PDFs when the page bytes, its dependencies and the
renderer configuration are unchanged.  Cache entries are content addressed and
therefore survive page insertion/reordering for notes whose page rendering is
independent of neighbouring pages.

Ordering is taken only from ``pageIdInfo.dat`` (Samsung's authoritative page
manifest).  Archive-entry filename sorting is never used as an ordering
source.  If the manifest is absent, ambiguous or disagrees with the CLI's
visible-page count, the caller falls back to the existing full renderer.

Limitations (documented tradeoffs):

* Embedded-PDF-paper notes are never rendered page-by-page.  The existing
  renderer appends an ink overlay companion; that is not a true composition of
  document paper and ink, so ``compile_note`` delegates to the full renderer
  and reports the limitation in ``warnings``.
* Media dependencies are not mapped from page records yet.  All non-page
  archive entries (``note.note``, ``media/*``, templates, end tag) are hashed
  into every page key.  Changing unrelated media therefore invalidates the
  whole note.  This is conservative and prevents stale pages.
* Archives whose ``note.note`` carries non-whitespace flowing body text have
  that text mapped across pages by the renderer.  For those notes an
  ordered-page layout fingerprint is added to every key, so insertion/reorder
  recompiles all pages; this is reported in ``warnings``.  Ink-only notes whose
  body text is empty do not get that fingerprint and reuse pages across
  insertion/reordering.  If the body cannot be classified, the ordered
  fingerprint is applied conservatively.
* A trailing blank compatibility record is omitted from the visible page set
  exactly when the CLI reports one fewer visible page than the manifest.  Any
  other count disagreement is rejected as an unsupported mapping rather than
  risking a wrong page splice.

No original PDF, VM state or production database is written by this module.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import time
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from typing import Callable, Iterator, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT / "02_extracted_notes_pdf"
HIST = ROOT / "ai_agend_history"
PAGE_CACHE_DIR = Path(os.environ.get("SN_PAGE_CACHE_DIR", str(HIST / "page_cache")))
CACHE_SCHEMA = "page-delta-v1"

PROJECT_CAP_BYTES = 55_000_000_000
FREE_FLOOR_BYTES = 15 * 1024**3
# A page cache is derived data; keep it small because the worktree is near cap.
PAGE_CACHE_LIMIT_BYTES = 256 * 1024**2

_PAGE_MANIFEST = "pageIdInfo.dat"
_INTEGRITY_BYTES = 32
_PREFIX_RE = re.compile(r"[0-9a-f]{2}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

SearchableHook = Callable[[Path, str, Path], Path | None]

__all__ = ["compile_note", "prune_cache", "PageDeltaError", "UnsupportedMapping",
           "SearchableHook"]


class PageDeltaError(RuntimeError):
    """A page-delta operation failed and no safe cache state was published."""


class UnsupportedMapping(PageDeltaError):
    """The source cannot be mapped to authoritative visible page UUIDs."""


def _ie():
    """Import the existing exporter lazily to avoid a circular import."""
    try:
        return importlib.import_module("incremental_export")
    except ModuleNotFoundError:
        return importlib.import_module(
            "02_extracted_notes_pdf.script.incremental_export"
        )


def _sha256_bytes(data: bytes) -> str:
    """Hash bytes with the SHA-256 digest used by the cache keys."""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash a file with constant memory use."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_json(value: object) -> str:
    """Stable digest for renderer configuration dictionaries."""
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _probe_archive(archive: Path) -> tuple[int, list[dict], list[str]]:
    """Delegate the CLI probe to the existing exporter (lazy import)."""
    return _ie().probe_archive(archive)


def _verify_pdf(path: Path, expected: int | None = None) -> int:
    """Verify a PDF through the existing exporter's structural validator."""
    return _ie().verify(path, expected)


def _content_pages(path: Path, pages: int, inked: list | None = None) -> int:
    """Run the existing exporter's ink check on the spliced output."""
    return _ie().content_pages(path, pages, inked)


def _execute(args: list[str], timeout: int = 180) -> str:
    """Run a bounded command through the existing exporter."""
    return _ie().execute(args, timeout)


def _cli() -> Path:
    """Resolve the sdocx CLI path from the existing exporter."""
    return Path(_ie().CLI)


@lru_cache(maxsize=4)
def _renderer_config() -> dict:
    """Describe the renderer settings that affect a single-page PDF."""
    cli = _cli()
    stat = cli.stat()
    try:
        version = _execute([str(cli), "--version"], 30).strip()
    except Exception:
        version = "unknown"
    return {
        "schema": CACHE_SCHEMA,
        "format": "pdf",
        "pdf_dpi": 96,
        "page_selection": "single-visible-page",
        "cli_path": str(cli),
        "cli_size": stat.st_size,
        "cli_mtime_ns": stat.st_mtime_ns,
        "cli_version": version,
    }


def _build_archive(source: Path, work: Path) -> Path:
    """Build a deterministic stored ZIP mirroring the existing renderer."""
    archive = work / "source.sdocx"
    with archive.open("wb") as raw:
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_STORED) as zf:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(source).as_posix())
    return archive


def _read_page_manifest(archive: Path) -> list[tuple[str, bytes]]:
    """Parse pageIdInfo.dat in Samsung's authoritative page order.

    Layout: 32 integrity bytes, little-endian u16 entry count, then each entry
    is a u16-length UTF-16LE page UUID followed by 32 integrity bytes.
    """
    with zipfile.ZipFile(archive) as zf:
        try:
            data = zf.read(_PAGE_MANIFEST)
        except KeyError as exc:
            raise UnsupportedMapping("pageIdInfo.dat is absent") from exc
    if len(data) < _INTEGRITY_BYTES + 2:
        raise UnsupportedMapping("pageIdInfo.dat is truncated")
    offset = _INTEGRITY_BYTES
    count = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    if count == 0:
        raise UnsupportedMapping("pageIdInfo.dat lists no pages")
    entries: list[tuple[str, bytes]] = []
    for _ in range(count):
        if offset + 2 > len(data):
            raise UnsupportedMapping("pageIdInfo.dat entry count is truncated")
        units = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        end = offset + units * 2
        if end + _INTEGRITY_BYTES > len(data):
            raise UnsupportedMapping("pageIdInfo.dat page UUID is truncated")
        try:
            page_id = data[offset:end].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise UnsupportedMapping("pageIdInfo.dat page UUID is not UTF-16") from exc
        if not page_id or page_id in (".", "..") or any(c in page_id for c in "/\\\x00:"):
            raise UnsupportedMapping("unsafe page identifier in page manifest")
        offset = end
        entries.append((page_id, data[offset:offset + _INTEGRITY_BYTES]))
        offset += _INTEGRITY_BYTES
    if len({page_id for page_id, _hash in entries}) != len(entries):
        raise UnsupportedMapping("pageIdInfo.dat contains duplicate page UUIDs")
    return entries


def _page_entry_names(archive: Path) -> dict[str, str]:
    """Map page UUID to ZIP entry name without relying on entry order."""
    names: dict[str, str] = {}
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.endswith(".page"):
                continue
            stem = PurePosixPath(info.filename).stem
            if stem in names:
                raise UnsupportedMapping(f"duplicate .page entry for {stem}")
            names[stem] = info.filename
    if not names:
        raise UnsupportedMapping("archive contains no .page entries")
    return names


def _ordered_visible_pages(
    archive: Path, count: int, page_info: Sequence[dict]
) -> list[str]:
    """Return visible page UUIDs in true order, or reject the mapping."""
    entries = _read_page_manifest(archive)
    names = _page_entry_names(archive)
    ids = [page_id for page_id, _hash in entries]
    missing = [page_id for page_id in ids if page_id not in names]
    if missing:
        raise UnsupportedMapping("manifest references missing .page entries: " + ",".join(missing[:4]))
    # pageIdInfo.dat is the authoritative visible order. Archives can retain
    # stale `.page` payloads that are not referenced by it; they are not visible
    # pages and must not poison an otherwise exact page-count mapping.
    indexes = [int(page.get("index", -1)) for page in page_info]
    if indexes != list(range(count)):
        raise UnsupportedMapping("CLI page indexes are not a complete 0-based range")
    if count == len(ids) - 1:
        # sdocx layout_document omits only its trailing blank compatibility
        # record.  The CLI count is authoritative; do not guess from names.
        ids = ids[:-1]
    elif count != len(ids):
        raise UnsupportedMapping(
            f"manifest has {len(ids)} pages but CLI sees {count} visible pages"
        )
    if not ids:
        raise UnsupportedMapping("no visible page UUIDs remain after layout mapping")
    return ids


def _page_bytes(archive: Path, page_id: str) -> bytes:
    """Read one page binary by authoritative UUID."""
    names = _page_entry_names(archive)
    name = names.get(page_id)
    if name is None:
        raise UnsupportedMapping(f"page {page_id} is not present in the archive")
    with zipfile.ZipFile(archive) as zf:
        return zf.read(name)


def _dependency_digest(archive: Path) -> tuple[str, list[str]]:
    """Hash every non-page archive entry conservatively.

    Dependencies are not mapped per page yet, so all media and note metadata
    participate in every page key.  The tradeoff is deliberate: unrelated media
    edits recompile the note instead of risking a stale page.
    """
    digest = hashlib.sha256()
    names: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for info in sorted(zf.infolist(), key=lambda item: item.filename):
            name = info.filename
            if info.is_dir() or name.endswith(".page") or name == _PAGE_MANIFEST:
                continue
            data = zf.read(info)
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(str(len(data)).encode("ascii") + b"\0")
            digest.update(data)
            names.append(name)
    return digest.hexdigest(), names


class _Cursor:
    """Small bounds-checked little-endian reader for Samsung note metadata."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes | memoryview, pos: int = 0) -> None:
        self.data = memoryview(data)
        self.pos = pos

    def take(self, size: int) -> memoryview:
        """Read a bounded slice, rejecting truncated records."""
        end = self.pos + size
        if size < 0 or end > len(self.data):
            raise ValueError("truncated Samsung binary record")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def u8(self) -> int:
        """Read one unsigned byte."""
        return int.from_bytes(self.take(1), "little")

    def u16(self) -> int:
        """Read one little-endian u16."""
        return int.from_bytes(self.take(2), "little")

    def i16(self) -> int:
        """Read one little-endian i16."""
        return int.from_bytes(self.take(2), "little", signed=True)

    def u32(self) -> int:
        """Read one little-endian u32."""
        return int.from_bytes(self.take(4), "little")

    def i64(self) -> int:
        """Read one little-endian i64."""
        return int.from_bytes(self.take(8), "little", signed=True)

    def mask(self) -> bytes:
        """Read Samsung's length-prefixed property/field mask."""
        return bytes(self.take(self.u8()))

    def utf16_u16(self) -> str:
        """Read a u16-length UTF-16LE string."""
        units = self.u16()
        return bytes(self.take(units * 2)).decode("utf-16-le", "replace")

    def utf16_u32(self) -> str:
        """Read a u32-length UTF-16LE string."""
        units = self.u32()
        return bytes(self.take(units * 2)).decode("utf-16-le", "replace")


def _read_frame(cursor: _Cursor) -> tuple[int, bytes, bytes]:
    """Read one native frame and return (kind, fields, flexible payload)."""
    size = cursor.u32()
    if size < 12:
        raise ValueError(f"invalid frame size {size}")
    frame = _Cursor(cursor.take(size - 4))
    kind = frame.i16()
    flexible_offset = frame.u32()
    frame.mask()  # properties
    fields = frame.mask()
    header = frame.pos
    fixed_end = size if flexible_offset == 0 and not any(fields) else flexible_offset
    if fixed_end < header + 4 or fixed_end > size:
        raise ValueError("frame flexible-data offset is outside the frame")
    return kind, fields, bytes(frame.data[fixed_end - 4:])


def _note_body_has_text(data: bytes) -> bool:
    """Return whether note.note's flowing body contains non-whitespace text."""
    note = _Cursor(data)
    fixed_end = note.u32()
    if fixed_end > len(data):
        raise ValueError("note flexible-data offset is outside the record")
    note.mask()  # properties
    note.mask()  # fields
    note.u32()  # format version
    note.utf16_u16()  # note id
    note.u32()  # file revision
    note.i64()  # created timestamp
    note.i64()  # modified timestamp
    for _ in range(5):  # width, height, horizontal/vertical padding, minimum version
        note.u32()
    note.take(note.u32())  # title object
    body = note.take(note.u32())  # body rich-text object

    body_cursor = _Cursor(body)
    kind, _fields, _flex = _read_frame(body_cursor)  # object metadata
    if kind != 0:
        raise ValueError("note body object metadata frame missing")
    kind, _fields, _flex = _read_frame(body_cursor)  # shape
    if kind != 6:
        raise ValueError("note body shape frame missing")
    kind, fields, flexible = _read_frame(body_cursor)  # shape text
    if kind != 7:
        raise ValueError("note body shape-text frame missing")
    if not fields or not fields[0] & 1:
        return False
    flexible_cursor = _Cursor(flexible)
    common = flexible_cursor.take(flexible_cursor.u32())
    common_cursor = _Cursor(common)
    text = common_cursor.utf16_u32()
    return bool(text.strip())


def _note_layout_sensitive(archive: Path) -> bool | None:
    """True/False when note.note is parseable; None means be conservative."""
    with zipfile.ZipFile(archive) as zf:
        try:
            data = zf.read("note.note")
        except KeyError:
            return False
    try:
        return _note_body_has_text(data)
    except Exception:
        return None


def _page_key(
    page_id: str,
    page: bytes,
    dependency_digest: str,
    renderer_digest: str,
    layout_fingerprint: str,
) -> str:
    """Content-address one page, its dependencies and renderer settings."""
    digest = hashlib.sha256()
    digest.update(CACHE_SCHEMA.encode("ascii") + b"\0")
    digest.update(page_id.encode("utf-8") + b"\0")
    digest.update(_sha256_bytes(page).encode("ascii") + b"\0")
    digest.update(dependency_digest.encode("ascii") + b"\0")
    digest.update(renderer_digest.encode("ascii") + b"\0")
    digest.update(layout_fingerprint.encode("ascii") + b"\0")
    return digest.hexdigest()


def _entry_dir(key: str) -> Path:
    """Shard cache entries by hash prefix to keep directories bounded."""
    return PAGE_CACHE_DIR / CACHE_SCHEMA / key[:2] / key


def _note_manifest_path(note_key: str) -> Path:
    """Path of the atomic per-note manifest used for audit/validation."""
    return PAGE_CACHE_DIR / CACHE_SCHEMA / "notes" / f"{note_key}.json"


def _note_manifest_matches(path: Path, entries: Sequence[dict]) -> bool:
    """Validate a previous note manifest's count, keys and hashes."""
    try:
        data = json.loads(path.read_text())
        pages = data.get("pages", [])
        if data.get("schema") != CACHE_SCHEMA or data.get("count") != len(entries):
            return False
        if len(pages) != len(entries):
            return False
        return all(
            old.get("uuid") == new.get("uuid")
            and old.get("key") == new.get("key")
            and old.get("sha256") == new.get("sha256")
            for old, new in zip(pages, entries)
        )
    except Exception:
        return False


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write a file atomically and fsync it before publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy a derived file into the cache atomically."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with src.open("rb") as reader, tmp.open("wb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _quarantine(path: Path) -> None:
    """Move a corrupt cache entry out of the lookup path, then delete it."""
    if not path.exists():
        return
    trash = path.with_name(f"{path.name}.corrupt.{time.time_ns()}")
    try:
        path.rename(trash)
        shutil.rmtree(trash, ignore_errors=True)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def _tree_bytes(root: Path) -> int:
    """Size of a tree in bytes; metadata only, no file reads."""
    total = 0
    if not root.exists():
        return 0
    for base, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += (Path(base) / name).lstat().st_size
            except OSError:
                continue
    return total


def _free_bytes(path: Path) -> int:
    """Free space at the nearest existing ancestor of a cache path."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


class _WriteBudget:
    """Enforce the 55 GB workspace ceiling, 15 GiB floor and cache ceiling."""

    def __init__(self) -> None:
        self.project_bytes = _tree_bytes(PROJECT_ROOT)
        self.cache_bytes = _tree_bytes(PAGE_CACHE_DIR)
        self.written = 0
        self.reason = ""

    def allow(self, additional: int) -> bool:
        """Return whether a cache write is safe, recording the first refusal."""
        self.reason = "cache write refused"
        if self.cache_bytes + self.written + additional > PAGE_CACHE_LIMIT_BYTES:
            self.reason = "page cache limit would be exceeded"
            return False
        free = _free_bytes(PAGE_CACHE_DIR.parent)
        if free - additional < FREE_FLOOR_BYTES:
            self.reason = "free-space floor (15 GiB) would be breached"
            return False
        cache_inside_workspace = PAGE_CACHE_DIR.resolve().is_relative_to(PROJECT_ROOT.resolve())
        if cache_inside_workspace and self.project_bytes + self.written + additional > PROJECT_CAP_BYTES:
            self.reason = "workspace ceiling (55 GB) would be exceeded"
            return False
        return True

    def commit(self, size: int) -> None:
        """Account for a completed cache write."""
        self.written += size


def _iter_shard_entries(schema_root: Path) -> Iterator[Path]:
    """Yield only cache-owned SHA256 shard directories under the schema root.

    A cache-owned entry is exactly ``<root>/<2-hex>/<64-hex>/`` with no symlinks
    in the path. Manifests, locks, temp files and unknown names are ignored.
    """
    try:
        if not schema_root.is_dir() or schema_root.is_symlink():
            return
        root_real = schema_root.resolve()
        for prefix in sorted(schema_root.iterdir()):
            if prefix.is_symlink() or not prefix.is_dir():
                continue
            if not _PREFIX_RE.fullmatch(prefix.name):
                continue
            for entry in sorted(prefix.iterdir()):
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if not _SHA256_RE.fullmatch(entry.name):
                    continue
                if not entry.resolve().is_relative_to(root_real):
                    continue
                yield entry
    except OSError:
        return


def _manifest_keys(schema_root: Path) -> set[str]:
    """Collect page keys referenced by structurally valid per-note manifests."""
    keys: set[str] = set()
    notes = schema_root / "notes"
    if not notes.is_dir() or notes.is_symlink():
        return keys
    try:
        paths = list(notes.glob("*.json"))
    except OSError:
        return keys
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
            continue
        pages = data.get("pages")
        if not isinstance(pages, list) or data.get("count") != len(pages):
            continue
        found: list[str] = []
        for page in pages:
            if not isinstance(page, dict):
                found = []
                break
            key = page.get("key")
            if not isinstance(key, str) or not _SHA256_RE.fullmatch(key):
                found = []
                break
            found.append(key)
        keys.update(found)
    return keys


def _entry_recency(entry: Path) -> int:
    """Nanosecond last-use stamp; zero for unreadable entries (evicted first)."""
    try:
        return entry.stat().st_mtime_ns
    except OSError:
        return 0


def _touch_entry(entry: Path) -> None:
    """Record a cache hit as recent use without rewriting cache metadata."""
    try:
        os.utime(entry, None)
    except OSError:
        pass


def prune_cache(max_bytes: int | None = None) -> dict:
    """Bound the page cache by evicting orphan then least-recently-used entries.

    Only SHA256 shard directories directly beneath ``PAGE_CACHE_DIR/CACHE_SCHEMA``
    are candidates. Keys named by valid per-note manifests are protected; while
    the cache is under the limit nothing is evicted. Once over the limit,
    unreferenced (orphan) entries go first, then protected entries in
    least-recently-used order, stopping as soon as the limit is met. Manifests,
    the lock, foreign paths and every file outside the page cache are untouched.

    Callers should hold ``_cache_lock`` to avoid racing a concurrent export.
    Returns counters plus the resulting byte total for auditing.
    """
    limit = PAGE_CACHE_LIMIT_BYTES if max_bytes is None else max(0, int(max_bytes))
    schema_root = PAGE_CACHE_DIR / CACHE_SCHEMA
    summary = {
        "scanned": 0, "evicted": 0, "freed_bytes": 0,
        "total_bytes": 0, "limit_bytes": limit,
    }
    entries = list(_iter_shard_entries(schema_root))
    summary["scanned"] = len(entries)
    total = _tree_bytes(PAGE_CACHE_DIR)
    summary["total_bytes"] = total
    if total <= limit:
        return summary
    protected = _manifest_keys(schema_root)
    sizes = {entry: _tree_bytes(entry) for entry in entries}
    candidates = sorted(
        entries,
        key=lambda entry: (entry.name in protected, _entry_recency(entry), entry.name),
    )
    for entry in candidates:
        if total <= limit:
            break
        try:
            shutil.rmtree(entry)
        except OSError:
            continue
        size = sizes[entry]
        total -= size
        summary["evicted"] += 1
        summary["freed_bytes"] += size
        try:
            entry.parent.rmdir()  # drop an emptied shard prefix
        except OSError:
            pass
    summary["total_bytes"] = total
    return summary


@contextmanager
def _cache_lock() -> Iterator[None]:
    """Serialize cache validation/publication across concurrent exports."""
    import fcntl

    lock_dir = PAGE_CACHE_DIR / CACHE_SCHEMA
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / ".lock").open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_cached_page(key: str) -> tuple[Path, dict] | None:
    """Load a validated cached page or quarantine a missing/corrupt entry."""
    entry = _entry_dir(key)
    pdf = entry / "page.pdf"
    meta_path = entry / "entry.json"
    try:
        meta = json.loads(meta_path.read_text())
        if meta.get("key") != key or meta.get("schema") != CACHE_SCHEMA:
            raise ValueError("cache key/schema mismatch")
        size = pdf.stat().st_size
        if size != int(meta["size"]) or size == 0:
            raise ValueError("cached page size mismatch")
        if _sha256_file(pdf) != meta["sha256"]:
            raise ValueError("cached page hash mismatch")
        if _verify_pdf(pdf, 1) != 1:
            raise ValueError("cached page is not a one-page PDF")
        _touch_entry(entry)
        return pdf, meta
    except Exception:
        _quarantine(entry)
        return None


def _store_cached_page(
    key: str,
    page_id: str,
    pdf: Path,
    budget: _WriteBudget,
    warnings: list[str],
    searchable_hook: SearchableHook | None,
    work: Path,
) -> dict | None:
    """Publish a page and optional searchable derivative into the cache."""
    size = pdf.stat().st_size
    if not budget.allow(size + 2048):
        if budget.reason not in warnings:
            warnings.append(f"page-delta: cache write skipped ({budget.reason})")
        return None
    entry = _entry_dir(key)
    entry.mkdir(parents=True, exist_ok=True)
    page_path = entry / "page.pdf"
    _atomic_copy(pdf, page_path)
    budget.commit(size + 2048)
    meta = {
        "schema": CACHE_SCHEMA,
        "key": key,
        "page_uuid": page_id,
        "sha256": _sha256_file(page_path),
        "size": page_path.stat().st_size,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "searchable": None,
    }
    if searchable_hook is not None:
        try:
            result = searchable_hook(page_path, page_id, work)
            if result is not None and Path(result).is_file():
                searchable = entry / "searchable.pdf"
                searchable_size = Path(result).stat().st_size
                if not budget.allow(searchable_size + 512):
                    warnings.append(
                        f"page-delta: searchable output skipped ({budget.reason})"
                    )
                else:
                    _atomic_copy(Path(result), searchable)
                    meta["searchable"] = {
                        "sha256": _sha256_file(searchable),
                        "size": searchable.stat().st_size,
                    }
                    budget.commit(searchable_size + 512)
        except Exception as exc:
            warnings.append(f"page-delta: searchable hook failed for {page_id}: {exc}")
    _atomic_write_bytes(
        entry / "entry.json",
        json.dumps(meta, sort_keys=True, indent=1).encode("utf-8"),
    )
    _touch_entry(entry)
    return meta


def _embedded_pdf_present(source: Path, page_info: Sequence[dict]) -> bool:
    """Detect notes whose paper is an embedded PDF (never delta-rendered)."""
    media = source / "media"
    if media.is_dir() and any(path.is_file() and path.suffix.lower() == ".pdf"
                              for path in media.glob("*.pdf")):
        return True
    return any("PDF" in str(page.get("template", "")) for page in page_info)


def _searchable_cached_page(key: str, page_id: str, raw: Path, meta: dict | None,
                            hook: SearchableHook | None, work: Path,
                            budget: _WriteBudget, warnings: list[str]) -> Path | None:
    """Use searchable pages even when the optional persistent cache is full.

    Cache capacity must never decide whether the published PDF gets OCR. A
    newly generated searchable page can live in per-note scratch until splice.
    """
    if hook is None:
        return None
    profile = str(getattr(hook, "cache_key", "searchable-v1"))
    entry = _entry_dir(key)
    target = entry / "searchable.pdf"
    stored = (meta or {}).get("searchable") or {}
    try:
        if (stored.get("profile") == profile and target.stat().st_size == stored.get("size")
                and _sha256_file(target) == stored.get("sha256") and _verify_pdf(target, 1) == 1):
            _touch_entry(entry)
            return target
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        result = hook(raw, page_id, work)
        if result is None:
            return None
        result = Path(result)
        _verify_pdf(result, 1)
        if meta is None:
            warnings.append("page-delta: using transient searchable page (raw cache full)")
            return result
        if not budget.allow(result.stat().st_size + 1024):
            warnings.append(f"page-delta: searchable cache deferred ({budget.reason})")
            return result
        _atomic_copy(result, target)
        meta["searchable"] = {"sha256": _sha256_file(target), "size": target.stat().st_size,
                              "profile": profile}
        _atomic_write_bytes(entry / "entry.json", json.dumps(meta, sort_keys=True).encode())
        budget.commit(target.stat().st_size + 1024)
        return target
    except Exception as error:
        warnings.append(f"page-delta: searchable page deferred ({type(error).__name__})")
        return None


def _render_page_pdf(
    archive: Path,
    index: int,
    page_id: str,
    output: Path,
    warnings: list[str],
) -> None:
    """Render one visible page (1-based CLI index) and verify it."""
    try:
        output.unlink()
    except FileNotFoundError:
        pass
    log = _execute(
        [str(_cli()), str(archive), "--pages", str(index + 1), "-o", str(output)],
        300,
    )
    for warning in re.findall(r"Warning \[[^\]]+\][^\n]*", log):
        if warning not in warnings:
            warnings.append(warning)
    if _verify_pdf(output, 1) != 1:
        raise PageDeltaError(f"renderer produced {page_id} with a non-single page")


def _splice_pages(page_paths: Sequence[Path], output: Path, inked: list[int]) -> None:
    """Merge single-page PDFs in visible order and verify the result."""
    import pikepdf

    part = output.with_name(output.name + ".part")
    try:
        merged = pikepdf.Pdf.new()
        for page_path in page_paths:
            with pikepdf.open(page_path) as page:
                if len(page.pages) != 1:
                    raise PageDeltaError(f"cache page {page_path} is not one page")
                merged.pages.extend(page.pages)
        merged.save(
            part,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
        merged.close()
        _verify_pdf(part, len(page_paths))
        os.replace(part, output)
    finally:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
    _content_pages(output, len(page_paths), inked or None)


def _fallback(
    source: Path,
    work: Path,
    warnings: list[str],
    reason: str,
) -> tuple[Path, int, list[str]]:
    """Delegate to the existing full renderer, preserving its warnings."""
    warnings.append("page-delta-fallback: " + reason)
    try:
        output, pages, extra = _ie().render(source, work)
    except Exception as exc:
        raise PageDeltaError(f"page delta failed ({reason}); full render failed: {exc}") from exc
    for warning in extra:
        if warning not in warnings:
            warnings.append(warning)
    return output, pages, warnings


def compile_note(
    source: Path,
    work: Path,
    *,
    searchable_hook: SearchableHook | None = None,
) -> tuple[Path, int, list[str]]:
    """Compile a note through the page cache, falling back safely.

    Returns ``(original_pdf, page_count, warnings)`` and is interface-compatible
    with ``incremental_export.render``.  ``searchable_hook`` is optional and is
    called once per cache miss as ``hook(page_pdf, page_uuid, work) -> Path``.
    """
    source, work = Path(source), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    try:
        archive = _build_archive(source, work)
        count, page_info, probe_warnings = _probe_archive(archive)
        warnings.extend(probe_warnings)
        if count <= 0:
            raise UnsupportedMapping("renderer reports no visible pages")
        listed = {page_id for page_id, _integrity in _read_page_manifest(archive)}
        unlisted = sorted(set(_page_entry_names(archive)) - listed)
        if unlisted:
            warnings.append(
                f"page-delta: ignored {len(unlisted)} unlisted `.page` archive record(s) "
                "not present in pageIdInfo.dat"
            )
        page_ids = _ordered_visible_pages(archive, count, page_info)
        if _embedded_pdf_present(source, page_info):
            raise UnsupportedMapping(
                "embedded PDF paper requires the full renderer; "
                "the ink-overlay companion is not a true composition"
            )
        dependencies, _dependency_names = _dependency_digest(archive)
        renderer = _digest_json(_renderer_config())
        flow_text = _note_layout_sensitive(archive)
        layout_sensitive = flow_text is not False
        if flow_text is True:
            warnings.append(
                "page-delta: note.note contains flowing text; ordered layout "
                "fingerprint disables insertion/reorder reuse"
            )
        elif flow_text is None:
            warnings.append(
                "page-delta: note.note body could not be classified; using a "
                "conservative ordered layout fingerprint"
            )
        layout_fingerprint = (
            _sha256_bytes("\0".join(page_ids).encode("utf-8"))
            if layout_sensitive
            else ""
        )
        note_key = _sha256_bytes(
            ("\0".join(page_ids) + "|" + dependencies + "|" + renderer).encode("utf-8")
        )
        page_paths: list[Path] = []
        searchable_paths: list[Path | None] = []
        entries: list[dict] = []
        with _cache_lock():
            try:
                pruned = prune_cache()
                if pruned["evicted"]:
                    warnings.append(
                        f"page-delta: pruned {pruned['evicted']} cache entries "
                        f"({pruned['freed_bytes']} bytes)"
                    )
            except Exception as exc:
                warnings.append(f"page-delta: cache prune skipped ({type(exc).__name__})")
            budget = _WriteBudget()
            for index, page_id in enumerate(page_ids):
                page = _page_bytes(archive, page_id)
                key = _page_key(
                    page_id, page, dependencies, renderer, layout_fingerprint
                )
                cached = _load_cached_page(key)
                if cached is None:
                    rendered = work / f"page-delta-{index:06d}.pdf"
                    _render_page_pdf(archive, index, page_id, rendered, warnings)
                    meta = _store_cached_page(
                        key, page_id, rendered, budget, warnings, None, work
                    )
                    searchable_paths.append(_searchable_cached_page(
                        key, page_id, rendered, meta, searchable_hook, work, budget, warnings))
                    page_paths.append(rendered)
                    entries.append(
                        {"uuid": page_id, "key": key, "sha256": _sha256_file(rendered),
                         "size": rendered.stat().st_size,
                         "searchable": bool(meta and meta.get("searchable"))}
                    )
                else:
                    pdf, meta = cached
                    searchable_paths.append(_searchable_cached_page(
                        key, page_id, pdf, meta, searchable_hook, work, budget, warnings))
                    page_paths.append(pdf)
                    entries.append(
                        {"uuid": page_id, "key": key, "sha256": meta["sha256"],
                         "size": meta["size"],
                         "searchable": bool(meta.get("searchable"))}
                    )
        inked = [i + 1 for i, item in enumerate(page_info) if int(item.get("strokes", 0))]
        output = work / "page-delta-complete.pdf"
        _splice_pages(page_paths, output, inked)
        if searchable_hook is not None and all(searchable_paths):
            derived = work / "page-delta-searchable.pdf"
            _splice_pages([Path(p) for p in searchable_paths if p is not None], derived, inked)
            from derive_corpus import verify_pair
            verify_pair(output, derived)
        try:
            manifest_path = _note_manifest_path(note_key)
            if manifest_path.exists() and not _note_manifest_matches(manifest_path, entries):
                warnings.append("page-delta: stale cache manifest replaced")
            manifest = {
                "schema": CACHE_SCHEMA,
                "note_key": note_key,
                "count": len(entries),
                "pages": entries,
                "dependencies": dependencies,
                "renderer": renderer,
                "layout_sensitive": layout_sensitive,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _atomic_write_bytes(
                manifest_path,
                json.dumps(manifest, sort_keys=True, indent=1).encode("utf-8"),
            )
        except Exception as exc:
            warnings.append(f"page-delta: note manifest not published: {exc}")
        warnings.append(
            f"page-delta: reused {sum(1 for p in page_paths if p.parent != work)}"
            f"/{len(page_paths)} cached pages"
        )
        return output, len(page_paths), warnings
    except Exception as exc:
        return _fallback(source, work, warnings, f"{type(exc).__name__}: {exc}")
