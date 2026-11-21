"""Read-only incremental Samsung Notes export with bounded page chunks.

Uses the independently built sdocx-cli on Linux. Existing verified exports
are preserved until an entire replacement passes validation. Unsupported
rendering is a reported failure, never a successful blank export.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT / "02_extracted_notes_pdf"
HIST = ROOT / "ai_agend_history"
DERIVED = ROOT / "notes/compressed"
LEGACY_ORIGINAL = ROOT / "notes/original"  # never read, never sourced
STATE = HIST / "incremental_state.sqlite"
TRACKER = HIST / "export_tracker.sqlite"
UUID_NAME = re.compile(r"__([0-9a-fA-F-]{36})\.pdf$")
# Bump whenever the derivation loses structure (links, outline, labels). A row
# carrying an older marker is re-exported once even if bytes and mtime look fine.
STRUCTURE_REVISION = "stage3-structure-v1"
CANVAS_AUDIT = HIST / "pre_repair_canvas_audit.private.json"
# Guest cloud sync can add notes after an export starts.  Re-read the
# authoritative VM inventory after each batch and fold new notes in, with a
# hard cap so a continuously syncing guest cannot keep the exporter alive.
SYNC_RECHECK_PASSES = 3


SAM_NOTES_ROOT = Path(os.environ.get("SAM_NOTES_ROOT", str(PROJECT_ROOT.parent / "sam_notes"))).expanduser()
_OWNS_RUN_ROOT = not os.environ.get("SN_EXPORT_WORK")
DEFAULT_RUN_ROOT = SAM_NOTES_ROOT / f"_run_tmp_{os.getpid()}"
if _OWNS_RUN_ROOT:
    atexit.register(shutil.rmtree, DEFAULT_RUN_ROOT, ignore_errors=True)


# Raw note payloads and rendered candidates are transient external scratch; a
# single note is staged here, then removed on success or failure.
DEST = Path(
    os.environ.get("SN_EXPORT_WORK", str(DEFAULT_RUN_ROOT))
).expanduser() / "original"


def _resolve_cli() -> Path:
    """Prefer an explicit override, then the repository copy, then ~/.local/bin."""
    candidates = [
        os.environ.get("SAM_SDOCX_CLI"),
        PROJECT_ROOT / "tools/sdocx-cli",
        Path.home() / ".local/bin/sdocx-cli",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return Path.home() / ".local/bin/sdocx-cli"


CLI = _resolve_cli()
GUEST = "C:/Users/notes/AppData/Local/Packages/SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState"


def execute(args: list[str], timeout: int = 180) -> str:
    """Run a bounded command and expose failures rather than hiding them."""
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stdout + p.stderr)[-3000:])
    return p.stdout + p.stderr


def _password_from_file(path: Path) -> str | None:
    """Return a guest credential from an env file; never log its value."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"SAMSUNG_NOTES_PASSWORD", "SAM_PASSWORD"}:
            value = value.strip().strip("\"'")
            if value:
                return value
    return None


def guest_password() -> str:
    """Load the guest credential: env override, native store, then legacy .env.

    The native ``~/.config/sam_win/.env`` is authoritative; the project
    ``02_extracted_notes_pdf/.env`` is consulted only when the native file has
    no credential key. Values are never printed or placed in argv.
    """
    credential = os.environ.get("SAMSUNG_NOTES_PASSWORD") or os.environ.get("SAM_PASSWORD")
    if credential:
        return credential
    credential = _password_from_file(Path.home() / ".config/sam_win/.env")
    if credential:
        return credential
    credential = _password_from_file(ROOT / ".env")
    if credential:
        return credential
    raise RuntimeError("Set SAMSUNG_NOTES_PASSWORD in ~/.config/sam_win/.env")


def _remove_stale_control_sockets(control_dir: Path) -> None:
    """Remove only dead OpenSSH control sockets owned by this guest transport."""
    for path in control_dir.iterdir():
        try:
            if not stat.S_ISSOCK(path.lstat().st_mode):
                continue
        except OSError:
            continue
        try:
            probe = subprocess.run(
                ["ssh", "-O", "check", "-p", "2222", "-o", f"ControlPath={path}",
                 "notes@127.0.0.1"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode != 0:
            try:
                path.unlink()
            except OSError:
                pass


def transport(kind: str) -> list[str]:
    """Resolve guest credentials without ever exposing the password."""
    credential = guest_password()
    # sshpass reads an environment variable, avoiding a password in argv.
    os.environ["SSHPASS"] = credential
    # OpenSSH rejects control sockets beneath fuseblk/NTFS because directory
    # modes do not persist there. Keep this tiny, ephemeral socket on native
    # Linux storage; all bulk scratch remains under the external cache.
    control_dir = Path("/tmp/opencode/sam_win_ssh")
    control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    control_dir.chmod(0o700)
    _remove_stale_control_sockets(control_dir)
    base = ["sshpass", "-e", kind, "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=120",
            "-o", f"ControlPath={control_dir}/%C"]
    return base + (["-p", "2222", "notes@127.0.0.1"] if kind == "ssh"
                   else ["-O", "-P", "2222"])


def upload(path: Path, remote: str) -> None:
    """Transfer a helper without titles or other note data in shell arguments."""
    execute(transport("scp") + [str(path), f"notes@127.0.0.1:{remote}"])


def inventory(work: Path, uuid: str | None = None, *, upload_helper: bool = True) -> dict:
    """Query production SQLite strictly read-only; include full folder ancestry.

    Passing ``uuid`` filters inside the read-only query so a per-note before/
    after snapshot only hashes that note's payload instead of every payload in
    the VM. The filter is validated here and bound as a SQL parameter, never
    interpolated into the query text.
    """
    if uuid is not None and not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        raise ValueError("invalid UUID filter")
    helper = work / "inventory.py"
    helper.write_text('''import json,sqlite3,sys
from pathlib import Path
p=Path.home()/"AppData/Local/Packages/SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState/Storage.sqlite"
c=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True)
c.execute("PRAGMA query_only=ON")
only=sys.argv[1] if len(sys.argv)>1 else None
cats={r[0]:(r[1],r[2]) for r in c.execute("SELECT UUID,ParentUUID,DisplayName FROM CategoryTreeDB WHERE IsDeleted=0")}
sql="SELECT UUID,Title,CategoryUUID,LastModifiedAt FROM NoteDB WHERE DeletedStatus=0"
rows=c.execute(sql+(" AND UUID=?" if only else ""),(only,) if only else ()).fetchall()
items=[]
for u,t,k,m in rows:
 parts=[];seen=set()
 while k in cats and k not in seen:
  seen.add(k);k,n=cats[k]
  if n and n!="Folders":parts.append(n)
 wdoc=p.parent/'wdoc'/u
 payload=sum(f.stat().st_size for f in wdoc.rglob('*') if f.is_file()) if wdoc.is_dir() else 0
 items.append(dict(uuid=u,title=t or "Untitled",folder="/".join(reversed(parts)),modified=int(m),payload_bytes=payload))
print(json.dumps(items,ensure_ascii=True))
''')
    remote = "C:/Users/notes/incremental_inventory.py"
    # One upload per run is enough; the helper is immutable during that run.
    # Re-uploading it for every per-note before/after query needlessly opens
    # hundreds of extra password-authenticated SSH sessions on Windows.
    if upload_helper:
        upload(helper, remote)
    command = f"C:\\py\\python.exe {remote}"
    if uuid is not None:
        command += f" {uuid}"
    out = execute(transport("ssh") + [command], 120)
    line = next(line for line in out.splitlines() if line.startswith("["))
    return {r["uuid"]: r for r in json.loads(line)}


def safe_component(value: str) -> str:
    """Sanitize filesystem components including traversal and duplicate titles."""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip().strip(".")
    return value[:120] or "Untitled"


def digest(path: Path) -> str:
    """Hash files with constant-size buffers, including giant PDFs."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=256)
def _verified_pdf(path: str, identity: tuple[int, ...]) -> int:
    """Cache a full decode only for an unchanged filesystem object identity."""
    import pikepdf
    pdf_path = Path(path)
    with pdf_path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("bad PDF header")
        f.seek(max(0, pdf_path.stat().st_size - 2048))
        if b"%%EOF" not in f.read():
            raise ValueError("incomplete PDF")
    with pikepdf.open(pdf_path) as pdf:
        n = len(pdf.pages)
        problems = pdf.check_pdf_syntax()
        errors = [p for p in problems if "error" in str(p).lower()]
        if not n or errors:
            raise ValueError(f"PDF validation: pages={n}, errors={errors or problems}")
    return n


def verify(path: Path, expected: int | None = None) -> int:
    """Fully validate each unchanged PDF once; never reuse checks after a write.

    Repeated repair/reuse checks need not decode every large image again. The
    process-local cache key includes device, inode, size, mtime and ctime, and a
    second stat rejects a file replaced or modified while it was being checked.
    """
    path = Path(path).resolve()

    def identity() -> tuple[int, ...]:
        """Include change time so equal-sized rewrites invalidate validation."""
        info = path.stat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    before = identity()
    n = _verified_pdf(str(path), before)
    if identity() != before:
        _verified_pdf.cache_clear()
        raise ValueError("PDF changed during validation")
    if expected is not None and n != expected:
        raise ValueError(f"PDF validation: pages={n}, expected={expected}")
    return n


def content_pages(path: Path, pages: int, inked: list | None = None) -> int:
    """Rasterize sample pages; require real ink, not blank templates.

    Samples pages known to carry strokes when available: decorated blanks
    (borders, page numbers) can pass a blind first/middle/last sample while
    holding no content. Catches renderers that silently omit PDF paper or
    strokes. A low feature threshold accommodates a handful of S-Pen strokes
    on dark/custom-PDF templates while a pure-white output still fails.
    """
    from PIL import Image
    if inked:
        picks = sorted(set(inked))
        samples = [picks[0], picks[len(picks) // 2], picks[-1]]
    else:
        samples = sorted({1, (pages + 1) // 2, pages})
    ok = 0
    for p in samples:
        for stale in path.parent.glob("_check-*.png"):
            try:
                stale.unlink()
            except OSError:
                pass
        stem = path.parent / f"_check-{p}"
        execute(["pdftoppm", "-r", "72", "-png", "-f", str(p), "-l", str(p),
                 str(path), str(stem)], 120)
        # pdftoppm appends its own page suffix; accept whatever it wrote.
        found = sorted(path.parent.glob("_check-*.png"))
        if not found:
            raise ValueError("could not rasterize sample page")
        png = found[0]
        try:
            with Image.open(png).convert("RGB") as im:
                small = im.resize((240, 320))
                raw = small.tobytes()
                px = [raw[i:i + 3] for i in range(0, len(raw), 3)]
            bg = max(set(px), key=px.count)
            varied = sum(1 for v in px
                         if abs(v[0] - bg[0]) + abs(v[1] - bg[1])
                          + abs(v[2] - bg[2]) > 18)
            if varied >= 6:
                ok += 1
        finally:
            for stale in path.parent.glob("_check-*.png"):
                try:
                    stale.unlink()
                except OSError:
                    pass
    if not ok:
        raise ValueError("rendered pages are blank (no ink detected)")
    return ok


def probe_archive(archive: Path) -> tuple[int, list, list]:
    """Parse page/stroke/template inventory plus integrity; no rendering yet."""
    out = execute([str(CLI), str(archive), "--pages", "1", "--verify-integrity",
                   "-o", str(archive.parent / "probe.pdf")], 240)
    match = re.search(r"(\d+) visible page\(s\)", out)
    if not match:
        raise ValueError("no authoritative page count")
    count = int(match[1])
    if not count or re.search(r"[1-9]\d* mismatched", out):
        raise ValueError("unsupported or inconsistent source: " + out[:2000])
    pages = []
    for m in re.finditer(r"Page (\d+): [^\n]*?template ([^,]+), (\d+) strokes",
                         out):
        pages.append({"index": int(m[1]), "template": m[2].strip(),
                      "strokes": int(m[3])})
    warnings = sorted(set(re.findall(r"Warning \[[^\]]+\][^\n]*", out)))
    return count, pages, warnings


def embedded_pdfs(source: Path) -> list[Path]:
    """Source-document PDFs bundled under media/ (highest resolution available)."""
    media = source / "media"
    if not media.is_dir():
        return []
    return sorted(p for p in media.glob("*.pdf") if p.is_file())


def source_is_empty(source: Path, archive: Path) -> tuple[bool, int, int]:
    """Confirm a blank render is a genuinely empty note, not a lost-ink failure."""
    visible_pages, page_info, _warnings = probe_archive(archive)
    strokes = sum(int(page["strokes"]) for page in page_info)
    has_embedded_pdf = bool(embedded_pdfs(source))
    # Images/text/media without strokes are not empty notes. Fail closed.
    substantive_media = any(p.is_file() and p.suffix.lower() != ".dat"
                            for p in (source / "media").glob("*"))
    from page_delta import _note_layout_sensitive
    text_is_empty = _note_layout_sensitive(archive) is False
    is_empty = (visible_pages > 0 and bool(page_info) and strokes == 0
                and not has_embedded_pdf and not substantive_media and text_is_empty)
    return is_empty, visible_pages, strokes


def render(source: Path, work: Path, chunk: int = 8) -> tuple[Path, int, list]:
    """Render resumable page chunks; reject blanks and inconsistent sources.

    Template-size fallbacks are tolerated when strokes still draw; warnings
    are recorded for the report. Notes whose pages sit on embedded PDF paper
    keep their source document: pure-PDF notes publish it directly, and inked
    pages are additionally rendered as a verified overlay companion.
    Blank output is a failure, never a silent export.
    """
    archive = work / "source.sdocx"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as z:
        for p in sorted(source.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(source).as_posix())
    count, pages, warnings = probe_archive(archive)
    strokes = sum(p["strokes"] for p in pages)
    pdfs = embedded_pdfs(source)
    paper = [p for p in pages if "PDF" in p["template"]]
    if pdfs:
        # The legacy branch below appended ink pages after the source PDF rather
        # than composing paper and ink. Never publish that as an exact note.
        try:
            from pdf_template_recovery import recover
        except ImportError:
            raise ValueError("Embedded PDF paper needs verified template recovery; previous export retained") from None
        # Ink over embedded paper is composed as a transparent vector layer on
        # the pristine source page; a page whose background cannot be proved
        # removable still fails closed inside ``recover``.
        return recover(source, work, allow_ink_overlay=True)
    if not strokes:
        if len(pdfs) == 1:
            import pikepdf
            with pikepdf.open(pdfs[0]) as doc:
                actual = len(doc.pages)
            verify(pdfs[0], actual)
            return pdfs[0], actual, warnings + ["embedded-source-document"]
        # A note with no strokes may still contain an attached image or other
        # substantive media. Render those sources instead of mistaking their
        # zero-stroke page records for an empty note.
        media = source / "media"
        substantive_media = media.is_dir() and any(
            path.is_file() and path.suffix.lower() != ".dat"
            for path in media.iterdir()
        )
        from page_delta import _note_layout_sensitive
        if not substantive_media and _note_layout_sensitive(archive) is not True:
            raise ValueError("rendered pages are blank (no ink detected)")
    if pdfs and len(pdfs) == 1 and any(
            "PDF" in p["template"] for p in pages if p["strokes"]):
        # Ink over an embedded document: keep the source untouched and render
        # only inked pages as a verified overlay companion (full re-render
        # would rasterize — or omit — the source paper).
        import pikepdf
        verify(pdfs[0])
        inked = sorted(p["index"] + 1 for p in pages if p["strokes"])
        ranges, start, prev = [], inked[0], inked[0]
        for i in inked[1:]:
            if i == prev + 1:
                prev = i
            else:
                ranges.append((start, prev))
                start, prev = i, i
        ranges.append((start, prev))
        overlay = work / "ink-overlay.pdf"
        merged_ink = pikepdf.Pdf.new()
        for start, end in ranges:
            part = work / f"ink-{start}-{end}.pdf"
            log = execute([str(CLI), str(archive), "--pages",
                           f"{start}-{end}", "-o", str(part)], 300)
            for w in sorted(set(re.findall(r"Warning \[[^\]]+\][^\n]*", log))):
                if w not in warnings:
                    warnings.append(w)
            verify(part, end - start + 1)
            with pikepdf.open(part) as p:
                merged_ink.pages.extend(p.pages)
            print(f"  ink pages {start}-{end}", flush=True)
        merged_ink.save(overlay, compress_streams=True,
                        object_stream_mode=pikepdf.ObjectStreamMode.generate)
        merged_ink.close()
        verify(overlay, len(inked))
        content_pages(overlay, len(inked))
        primary = work / "embedded.pdf"
        shutil.copyfile(pdfs[0], primary)
        merged = pikepdf.Pdf.new()
        with pikepdf.open(primary) as a, pikepdf.open(overlay) as b:
            merged.pages.extend(a.pages)
            merged.pages.extend(b.pages)
        with pikepdf.open(primary) as a:
            base_count = len(a.pages)
        output = work / "complete.pdf"
        merged.save(output, compress_streams=True,
                    object_stream_mode=pikepdf.ObjectStreamMode.generate)
        merged.close()
        verify(output, base_count + len(inked))
        return output, base_count + len(inked), warnings + ["embedded-source+ink-overlay"]
    import pikepdf
    merged = pikepdf.Pdf.new()
    for start in range(1, count + 1, chunk):
        end = min(count, start + chunk - 1)
        part = work / f"pages-{start}-{end}.pdf"
        log = execute([str(CLI), str(archive), "--pages", f"{start}-{end}",
                       "-o", str(part)], 300)
        for w in sorted(set(re.findall(r"Warning \[[^\]]+\][^\n]*", log))):
            if w not in warnings:
                warnings.append(w)
        verify(part, end - start + 1)
        with pikepdf.open(part) as p:
            merged.pages.extend(p.pages)
        print(f"  pages {start}-{end}/{count}", flush=True)
    output = work / "complete.pdf"
    merged.save(output, compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate)
    merged.close()
    verify(output, count)
    inked = sorted(p["index"] + 1 for p in pages if p["strokes"])
    content_pages(output, count, inked or None)
    return output, count, warnings


def open_state(readonly: bool = False) -> sqlite3.Connection:
    """Seed recorded export epochs once; never refresh them merely by indexing.

    ``readonly`` is used by a direct ``--dry-run`` so planning cannot create,
    migrate or seed the state database.
    """
    if readonly:
        if not STATE.exists():
            c = sqlite3.connect(":memory:")
            c.execute("CREATE TABLE exports(uuid TEXT PRIMARY KEY, modified INTEGER, title TEXT, folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, pages INTEGER, original_sha256 TEXT, derived_path TEXT, derived_sha256 TEXT, derived_chars INTEGER, derived_renderer TEXT)")
            c.execute("""CREATE TABLE empty_sources(
                uuid TEXT PRIMARY KEY, modified INTEGER NOT NULL, title TEXT NOT NULL,
                folder TEXT NOT NULL, visible_pages INTEGER NOT NULL,
                checked_at TEXT NOT NULL, reason TEXT NOT NULL)""")
            return c
        c = sqlite3.connect(STATE.resolve().as_uri() + "?mode=ro", uri=True)
        columns = {row[1] for row in c.execute("PRAGMA table_info(exports)")}
        if {"original_sha256", "derived_path", "derived_sha256",
                "derived_chars", "derived_renderer"} <= columns:
            return c
        c.close()
        # A schema migration needs write access; fall through once.
    c = sqlite3.connect(STATE)
    c.execute("CREATE TABLE IF NOT EXISTS exports(uuid TEXT PRIMARY KEY, modified INTEGER, title TEXT, folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, pages INTEGER, original_sha256 TEXT, derived_path TEXT, derived_sha256 TEXT, derived_chars INTEGER, derived_renderer TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS empty_sources(
        uuid TEXT PRIMARY KEY, modified INTEGER NOT NULL, title TEXT NOT NULL,
        folder TEXT NOT NULL, visible_pages INTEGER NOT NULL,
        checked_at TEXT NOT NULL, reason TEXT NOT NULL
    )""")
    columns = {row[1] for row in c.execute("PRAGMA table_info(exports)")}
    for name, kind in (("original_sha256", "TEXT"), ("derived_path", "TEXT"),
                       ("derived_sha256", "TEXT"), ("derived_chars", "INTEGER"),
                       ("derived_renderer", "TEXT")):
        if name not in columns:
            c.execute(f"ALTER TABLE exports ADD COLUMN {name} {kind}")
    if TRACKER.exists():
        with sqlite3.connect(f"file:{TRACKER}?mode=ro", uri=True) as old:
            for u,m,t,f,p,h in old.execute("SELECT uuid,last_modified,title,folder_path,host_file_path,sha256 FROM notes WHERE export_status='SUCCESS'"):
                path = Path(p or "")
                if not path.is_absolute():
                    path = ROOT / path
                # Only canonical compressed outputs may seed the new state;
                # an external legacy path is never recorded or sourced.
                if path.is_file() and _under(path, DERIVED):
                    c.execute("INSERT OR IGNORE INTO exports(uuid,modified,title,folder,path,sha256,renderer,pages) VALUES(?,?,?,?,?,?,?,?)",
                              (u,m,t,f,str(path),h,"native-existing",None))
    c.commit()
    return c


def _under(path: Path, root: Path) -> bool:
    """True when ``path`` is inside ``root`` after resolving real parents."""
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def output_valid(path: Path | str | None, expected: int | None = None) -> bool:
    """A canonical output is a real PDF, never a symlink or a broken link."""
    if not path:
        return False
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        return False
    try:
        verify(candidate, expected)
    except Exception:
        return False
    return True


def _valid_uuid(uuid: object) -> bool:
    """True only for a bare UUID; never a path-like or shell-like string."""
    return isinstance(uuid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", uuid) is not None


def target_for(note: dict) -> Path:
    """Canonical compressed path derived from the current folder and title.

    Defense in depth: the UUID is validated before it can become part of a path
    component, and the composed target must remain under ``DERIVED``. Planning
    already routes malformed UUIDs to an explicit failure without calling this
    function, so a caller that skips that guard still cannot escape the tree.
    """
    uuid = note["uuid"]
    if not _valid_uuid(uuid):
        raise ValueError(f"invalid UUID for export target: {uuid!r}")
    # Preserve the guest's exact hierarchy. A note with no CategoryUUID is a
    # true Samsung Notes root note; never invent a hard-coded "Uncategorized"
    # folder that the guest database does not contain.
    folder = [safe_component(part) for part in note["folder"].split("/") if part]
    target = DERIVED.joinpath(*folder, safe_component(note["title"]) + f"__{uuid}.pdf")
    # Check the resolved parent, not the leaf: a legacy PDF symlink may itself
    # point outside the tree and must remain addressable so repair can report or
    # replace the link safely. Resolving the leaf here incorrectly rejects that
    # safe replacement. Any symlinked parent that escapes the tree still fails.
    try:
        root = DERIVED.resolve()
        workspace = PROJECT_ROOT.resolve()
        parent = target.parent.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("could not resolve export target safely") from error
    if not root.is_relative_to(workspace) or not parent.is_relative_to(root):
        raise ValueError("export target escapes the compressed tree")
    return target


def _stored_row(state: sqlite3.Connection, uuid: str) -> dict | None:
    row = state.execute(
        "SELECT uuid,modified,title,folder,path,sha256,pages,original_sha256,derived_path,"
        "derived_sha256,derived_chars,derived_renderer,renderer FROM exports WHERE uuid=?",
        (uuid,)).fetchone()
    if not row:
        return None
    keys = ("uuid", "modified", "title", "folder", "path", "sha256", "pages",
            "original_sha256", "derived_path", "derived_sha256", "derived_chars",
            "derived_renderer", "renderer")
    return dict(zip(keys, row))


def existing_output(row: dict | None, uuid: str) -> Path | None:
    """First verified local standalone output for a UUID (never a symlink).

    When the row records a derived digest, a structurally valid but different
    file is treated as tampered and forces a re-export instead of being reused.
    A malformed UUID is rejected before it can reach ``rglob`` as a pattern.
    """
    if not _valid_uuid(uuid):
        raise ValueError(f"invalid UUID for output lookup: {uuid!r}")
    digests: dict[str, str] = {}
    if row:
        for candidate in (row.get("path"), row.get("derived_path")):
            if candidate and row.get("derived_sha256"):
                digests[str(Path(candidate).resolve())] = row["derived_sha256"]
        for candidate in (row.get("path"), row.get("derived_path")):
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_symlink() or not _under(path, DERIVED):
                continue
            if not output_valid(path):
                continue
            expected = digests.get(str(path.resolve()))
            if expected and digest(path) != expected:
                continue
            return path
    if DERIVED.is_dir():
        for path in sorted(DERIVED.rglob(f"*__{uuid}.pdf")):
            if not path.name.endswith(f"__{uuid}.pdf") or not output_valid(path):
                continue
            expected = digests.get(str(path.resolve()))
            if expected and digest(path) != expected:
                continue
            return path
    return None


def needs_export(row: dict | None, note: dict) -> bool:
    """New, changed, stale-structure, or absent/corrupt output always re-plans."""
    if row is None:
        return True
    if row["modified"] != note["modified"] or row["title"] != note["title"] \
            or row["folder"] != note["folder"]:
        return True
    if row.get("derived_renderer") != STRUCTURE_REVISION:
        # Older derivations may have dropped links/outline; only a fresh export
        # from the source can restore them, so re-plan once.
        return True
    return existing_output(row, note["uuid"]) is None


def known_uuid_path(path: Path | str | None, uuid: str) -> bool:
    """Only paths this exporter owns may ever be pruned."""
    if not path:
        return False
    candidate = Path(path)
    # Resolve the directory, never the candidate itself: a broken symlink still
    # lives inside the compressed tree even though its target is gone.
    return candidate.name.endswith(f"__{uuid}.pdf") and _under(candidate.parent, DERIVED)


def _prune_empty_dirs(directory: Path) -> None:
    """Remove now-empty descendant folders, stopping at the compressed root."""
    current = Path(directory)
    root = DERIVED.resolve()
    while True:
        try:
            if current.resolve() == root or not _under(current, DERIVED):
                return
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _archive_pdf_path(path: Path, category: str) -> Path:
    """Move a superseded PDF/link into ignored private history without deletion."""
    if not (path.is_file() or path.is_symlink()):
        raise FileNotFoundError(f"not an export file: {path}")
    try:
        relative = path.relative_to(DERIVED)
    except ValueError:
        raise ValueError(f"refusing to archive outside compressed tree: {path}") from None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns()}"
    destination = HIST / "preserved_originals" / category / stamp / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination = destination.with_name(
            f"{destination.stem}-{time.time_ns()}{destination.suffix}"
        )
    os.replace(path, destination)
    return destination


def prune_old(path: Path | str | None, uuid: str, keep: Path) -> bool:
    """Archive an old known UUID path after canonical publication; never delete."""
    if not known_uuid_path(path, uuid):
        return False
    candidate = Path(path)
    if candidate == Path(keep):
        return False
    try:
        _archive_pdf_path(candidate, "superseded_exports")
    except OSError:
        return False
    _prune_empty_dirs(candidate.parent)
    return True


def prune_uuid_aliases(uuid: str, keep: Path) -> list[str]:
    """Archive every other known ``*__<uuid>.pdf`` alias after success.

    Only UUID-suffixed files under the compressed tree are considered; files
    without a UUID suffix, other UUIDs, and non-files are never touched. No PDF
    bytes are deleted, including identical copies.
    """
    archived: list[str] = []
    if not DERIVED.is_dir():
        return removed
    keep = Path(keep)
    for path in sorted(DERIVED.rglob(f"*__{uuid}.pdf")):
        if path == keep or not known_uuid_path(path, uuid):
            continue
        try:
            if path.is_symlink() or path.is_file():
                archived.append(str(_archive_pdf_path(path, "superseded_aliases")))
                _prune_empty_dirs(path.parent)
        except OSError:
            continue
    return archived


def _record(state: sqlite3.Connection, note: dict, target: Path, sha: str,
            source_sha: str | None, pages: int, chars: int) -> None:
    """Store the canonical compressed path and its derived hash separately."""
    state.execute(
        """INSERT INTO exports(uuid,modified,title,folder,path,sha256,renderer,pages,
               original_sha256,derived_path,derived_sha256,derived_chars,derived_renderer)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(uuid) DO UPDATE SET
               modified=excluded.modified, title=excluded.title, folder=excluded.folder,
               path=excluded.path, sha256=excluded.sha256, renderer=excluded.renderer,
               pages=excluded.pages,
               original_sha256=COALESCE(excluded.original_sha256, exports.original_sha256),
               derived_path=excluded.derived_path, derived_sha256=excluded.derived_sha256,
               derived_chars=excluded.derived_chars,
               derived_renderer=excluded.derived_renderer""",
        (note["uuid"], note["modified"], note["title"], note["folder"], str(target), sha,
         STRUCTURE_REVISION, pages, source_sha, str(target), sha, chars, STRUCTURE_REVISION))
    state.commit()


def _prepare_staging() -> None:
    """Reset the transient raw staging tree without following any symlink."""
    if DEST.is_symlink():
        DEST.unlink()
    DEST.mkdir(parents=True, exist_ok=True)
    for child in DEST.iterdir():
        if child.is_symlink():
            child.unlink()
        elif child.is_dir() and re.fullmatch(r"[0-9a-fA-F-]{36}", child.name):
            shutil.rmtree(child, ignore_errors=True)


def _work_root() -> Path:
    """Scratch stays in an ephemeral sibling sam_notes run directory."""
    configured = Path(os.environ.get("SN_EXPORT_WORK", str(DEFAULT_RUN_ROOT)))
    root = configured.resolve()
    production_root = _under(root, SAM_NOTES_ROOT.resolve()) and root.name.startswith("_run_tmp_")
    isolated_test_root = (
        bool(os.environ.get("PYTEST_CURRENT_TEST"))
        and _under(root, Path("/tmp/opencode").resolve())
    )
    if not (production_root or isolated_test_root):
        raise ValueError(
            f"SN_EXPORT_WORK must be an ephemeral sam_notes run directory: {root}"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _note_signature(note: dict) -> tuple:
    """Identity that changes when the guest edits or moves an active note."""
    return (note.get("modified"), note.get("title"), note.get("folder"),
            note.get("payload_bytes"))


def _write_live_snapshot(live: dict) -> None:
    """Persist the authoritative VM inventory for later reconciliation."""
    HIST.mkdir(parents=True, exist_ok=True)
    snapshot = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active": len(live), "notes": list(live.values())}
    part = HIST / "live_inventory.private.tmp"
    part.write_text(json.dumps(snapshot, indent=2))
    os.replace(part, HIST / "live_inventory.private.json")


def _unreconciled(live: dict, processed: dict) -> list[str]:
    """Active UUIDs whose handled signature differs from the authoritative one.

    A UUID is reconciled only when its exact signature (modified, title, folder
    and payload) was processed; a mere UUID match is never enough. This is the
    single source of truth for both follow-up passes and the final pending list.
    """
    return sorted(u for u, note in live.items()
                  if _note_signature(note) != processed.get(u))


def _verified_empty_records(state: sqlite3.Connection, live: dict) -> list[dict]:
    """List only previously proven empty sources that still match live metadata."""
    rows: list[dict] = []
    for uuid, note in live.items():
        record = state.execute(
            "SELECT modified,title,folder,visible_pages,reason FROM empty_sources WHERE uuid=?",
            (uuid,),
        ).fetchone()
        if record and record[:3] == (note["modified"], note["title"], note["folder"]):
            rows.append({**note, "visible_pages": int(record[3]), "reason": str(record[4]),
                         "status": "verified_empty"})
    return rows


def _clear_deferred_failure(report: dict, uuid: str) -> None:
    """Forget a transient 'source changed; deferred' failure after a later pass
    successfully publishes that UUID's current signature.

    Letting the stale deferral survive would make a fully reconciled run exit
    nonzero even though no note was ultimately left behind.
    """
    report["failed"] = [item for item in report["failed"]
                        if item.get("uuid") != uuid
                        or "deferred" not in str(item.get("reason", ""))]


def _plan_batch(state: sqlite3.Connection, live: dict, dry_run: bool,
                uuids: list[str] | None, limit: int | None) -> list[dict]:
    """Plan one batch from a live, authoritative VM inventory snapshot.

    Local state is advisory only: a note absent from it is new and must still
    be exported.  Only a verified empty-source record that still matches the
    current note may suppress an otherwise active note.
    """
    planned: list[dict] = []
    for u, note in live.items():
        if not _valid_uuid(u):
            # Route a malformed identifier to an explicit export failure. Never
            # let it reach target_for()/existing_output(): those build a path or
            # an rglob pattern from the raw value and could probe or write
            # outside the compressed tree.
            planned.append(note)
            continue
        empty = state.execute(
            "SELECT modified,title,folder FROM empty_sources WHERE uuid=?", (u,)
        ).fetchone()
        if empty and empty == (note["modified"], note["title"], note["folder"]):
            continue
        if empty and not dry_run:
            state.execute("DELETE FROM empty_sources WHERE uuid=?", (u,))
        if not needs_export(_stored_row(state, u), note):
            continue
        planned.append(note)

    def priority(note: dict) -> tuple[bool, int, str]:
        """Prioritize visible missing notes without expensive PDF rechecks.

        Invalid UUIDs sort with missing outputs but never touch the filesystem:
        they exist only so _export_note can report an explicit failure.
        """
        if not _valid_uuid(note["uuid"]):
            return (False, -int(note["modified"]), note["uuid"])
        target = target_for(note)
        return (target.is_file() and not target.is_symlink(),
                -int(note["modified"]), note["uuid"])

    planned.sort(key=priority)
    if uuids:
        requested = set(uuids)
        planned = [note for note in planned if note["uuid"] in requested]
    if limit:
        planned = planned[:limit]
    return planned


def _export_note(state: sqlite3.Connection, work: Path, note: dict,
                 dry_run: bool, report: dict) -> str:
    """Stage, render, derive and publish one note; always clear its scratch."""
    u = note["uuid"]
    print(f"EXPORT {note['folder']}/{note['title']} ({u})", flush=True)
    # Validate before the UUID ever becomes a path component.
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", u):
        if not dry_run:
            report["failed"].append({**note, "reason": "invalid UUID"})
        print(f"FAILED {note['title']}: invalid UUID", flush=True)
        return "invalid"
    if dry_run:
        return "dry"
    note_work = work / u
    staging = DEST / u
    row = _stored_row(state, u)
    try:
        # Content unchanged, only folder/title moved: reuse the real verified
        # output instead of re-rendering the note. A stale structure marker
        # always forces a fresh source export.
        if (row and row["modified"] == note["modified"]
                and row.get("derived_renderer") == STRUCTURE_REVISION):
            reusable = existing_output(row, u)
            if reusable:
                target = target_for(note)
                target.parent.mkdir(parents=True, exist_ok=True)
                if reusable != target:
                    previous = None
                    if target.exists() or target.is_symlink():
                        previous = _archive_pdf_path(target, "canonical_collisions")
                    try:
                        os.replace(reusable, target)
                    except Exception:
                        if previous is not None and not (target.exists() or target.is_symlink()):
                            os.replace(previous, target)
                        raise
                pages = verify(target)
                sha = digest(target)
                chars = row.get("derived_chars") or text_chars(target)
                source_sha = row.get("original_sha256") or row.get("sha256")
                _record(state, note, target, sha, source_sha, pages, chars)
                prune_old(row.get("path"), u, target)
                prune_old(row.get("derived_path"), u, target)
                report["updated"].append({**note, "path": str(target), "pages": pages,
                                          "sha256": sha, "reused": True, "warnings": []})
                print(f"REUSED {target}", flush=True)
                return "reused"
        from derive_corpus import guard
        # Stage + render + derive need transient headroom; refuse before
        # transfer rather than exceed the 55 GB ceiling.
        reserve = int(note.get("payload_bytes", 0)) * 4 + 16 * 1024**2
        try:
            guard(reserve)
        except RuntimeError as error:
            raise RuntimeError(
                f"insufficient headroom for a single raw note "
                f"(payload={int(note.get('payload_bytes', 0))} bytes, "
                f"need={reserve} bytes): {error}") from None
        # ``note`` is the authoritative per-note snapshot from the current
        # batch. Avoid another remote login before the transfer; the post-copy
        # filtered inventory below still rejects a note that changed while it
        # was being staged/rendered.
        before = note
        staging.mkdir(parents=True, exist_ok=True)
        note_work.mkdir(parents=True, exist_ok=False)
        remote = f"notes@127.0.0.1:{GUEST}/wdoc/{u}"
        execute(transport("scp") + ["-r", remote, str(DEST)], 600)
        from page_delta import compile_note
        from derive_corpus import derive_file, page_ocr_hook, verify_pair
        # Each note owns a fresh work directory so a searchable cache from a
        # previous note can never be picked up by the fallback.
        output, pages, warnings = compile_note(staging, note_work,
                                               searchable_hook=page_ocr_hook)
        after = inventory(work, u, upload_helper=False).get(u)
        if after != before:
            raise RuntimeError("source changed during export; deferred, old PDF retained")
        cached = note_work / "page-delta-searchable.pdf"
        has_cache = cached.is_file() and not cached.is_symlink()
        # A cached full searchable PDF already carries OCR. The fallback full
        # render does not, so derive must OCR only its textless pages.
        source_pdf = cached if has_cache else output
        if has_cache:
            try:
                verify_pair(output, cached)
            except Exception as error:
                # The persistent page cache is an optimization, never the
                # source of truth. If its OCR derivative changes visible
                # content or navigation, discard it for this note and derive
                # from the freshly compiled authoritative output instead.
                warnings.append(
                    f"page-delta: rejected cached searchable output "
                    f"({type(error).__name__}: {str(error)[:180]})"
                )
                has_cache = False
                source_pdf = output
                print(f"CACHE_REJECTED {u}: {type(error).__name__}", flush=True)
        # A recovered embedded document owns its source-paper colors; its
        # non-PDF template pages were normalized by pdf_template_recovery.
        # Native rendered note canvases are normalized before OCR/derivation
        # and before their final visible-content comparison.
        normalization_warnings: list[str] = []
        if not embedded_pdfs(staging):
            from derive_corpus import normalize_light_canvas
            source_pdf, normalization_warnings = normalize_light_canvas(
                source_pdf, note_work
            )
            warnings.extend(normalization_warnings)
        target = target_for(note)
        ocr_requested = not has_cache
        result = derive_file(source_pdf, target=target, ocr=ocr_requested)
        if result.get("status") not in ("searchable", "no_text"):
            raise RuntimeError(f"derive status {result.get('status')}")
        if ocr_requested and int(result.get("sparse_pages", 0)) > 0 \
                and not result.get("ocr"):
            raise RuntimeError("OCR requested but no page was recognized")
        if target.is_symlink() or not target.is_file():
            raise ValueError("derived output is not a real file")
        derived_pages = verify(target)
        if int(result.get("pages", -1)) != derived_pages:
            raise ValueError("derived page count changed before publication")
        sha = digest(target)
        if sha != result.get("sha256"):
            raise ValueError("derived digest changed before publication")
        _record(state, note, target, sha, result.get("source_sha256"), derived_pages,
                int(result.get("chars", 0)))
        state.execute("DELETE FROM empty_sources WHERE uuid=?", (u,))
        state.commit()
        # Authoritative success (before == after live inventory): archive every
        # other UUID alias; even a differing historical PDF is never deleted.
        archived_aliases = prune_uuid_aliases(u, target)
        report["updated"].append({**note, "path": str(target), "pages": derived_pages,
                                  "sha256": sha, "warnings": warnings,
                                  "ocr": bool(result.get("ocr")),
                                   "archived_aliases": archived_aliases,
                                  "derived_from_page_cache": has_cache})
    except Exception as e:
        archive = note_work / "source.sdocx"
        source = staging
        if "rendered pages are blank (no ink detected)" in str(e) and archive.is_file():
            try:
                is_empty, visible_pages, strokes = source_is_empty(source, archive)
            except Exception:
                is_empty, visible_pages, strokes = False, 0, 0
            if is_empty:
                state.execute(
                    """INSERT OR REPLACE INTO empty_sources
                    (uuid,modified,title,folder,visible_pages,checked_at,reason)
                    VALUES(?,?,?,?,?,?,?)""",
                    (u, note["modified"], note["title"], note["folder"], visible_pages,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "no ink records or embedded PDF"),
                )
                state.commit()
                report["skipped_empty"].append({**note, "visible_pages": visible_pages})
                report["verified_empty"].append({
                    **note,
                    "visible_pages": visible_pages,
                    "reason": "zero strokes and no embedded or substantive media",
                })
                print(f"SKIPPED_EMPTY {u} visible_pages={visible_pages}; prior export retained",
                      flush=True)
                return "empty"
        report["failed"].append({**note, "reason": str(e)})
        print(f"FAILED {note['title']}: {str(e)[:300]}", flush=True)
        return "failed"
    finally:
        # The raw payload and per-note render cache are scratch: drop both
        # after success or failure so the guard measures steady state and
        # nothing leaks into the next note.
        shutil.rmtree(note_work, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    return "ok"


def run(limit: int | None, dry_run: bool, uuids: list[str] | None = None) -> int:
    """Export every active note from the authoritative VM inventory.

    The inventory is re-read after each batch: notes that sync into the VM
    after export starts are folded into a bounded follow-up pass instead of
    being silently omitted. Local state is advisory; it can suppress a note
    only through a verified, still-current empty-source record.
    """
    if not dry_run:
        HIST.mkdir(exist_ok=True)
    lock_ctx = contextlib.nullcontext() if dry_run else (HIST / "incremental.lock").open("w")
    with lock_ctx as lock:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if dry_run:
            state = open_state(readonly=True)
            work_root = None
        else:
            state = open_state()
            work_root = _work_root()
            _prepare_staging()
        # fuseblk queues unlinks asynchronously: rmtree can report ENOTEMPTY
        # for an already-empty directory. Never fail a successful export on it.
        tmp_ctx = (tempfile.TemporaryDirectory(prefix="sn-incremental-dry-",
                                               ignore_cleanup_errors=True) if dry_run
                   else tempfile.TemporaryDirectory(prefix="sn-incremental-", dir=work_root,
                                                    ignore_cleanup_errors=True))
        with tmp_ctx as tmp:
            work = Path(tmp)
            report = {"active": 0, "planned": 0, "updated": [], "failed": [],
                      "skipped_empty": [], "verified_empty": [], "dry_run": dry_run}
            # Track the exact signature handled for each UUID: a note edited
            # or moved mid-run is a different record and must be reprocessed,
            # not silently treated as complete because its UUID was seen.
            processed: dict[str, tuple] = {}
            planned_uuids: set[str] = set()
            sync_passes = 0
            recheck_failed = False
            recheck_error: str | None = None
            # --limit is an explicit partial-run knob: do not silently
            # process more notes in an automatic follow-up pass.
            max_passes = 1 if limit else SYNC_RECHECK_PASSES
            live = inventory(work)
            initial_active = len(live)
            report["initial_active"] = initial_active
            if not dry_run:
                # Persist the source inventory independently of the final run
                # report, so new/moved notes can be reconciled even mid-run.
                _write_live_snapshot(live)
            for pass_index in range(max_passes):
                batch = {u: note for u, note in live.items()
                         if _note_signature(note) != processed.get(u)}
                if not batch:
                    break
                # Plan without the caller's cap, then apply it explicitly so
                # only notes that were actually handled are marked processed.
                # A note deferred by --limit keeps its prior signature and can
                # never be mistaken for a completed UUID.
                full_plan = _plan_batch(state, batch, dry_run, uuids, None)
                planned = full_plan[:limit] if limit else full_plan
                deferred_by_limit = ({n["uuid"] for n in full_plan[limit:]}
                                     if limit else set())
                for u, note in batch.items():
                    if u not in deferred_by_limit:
                        processed[u] = _note_signature(note)
                planned_uuids.update(note["uuid"] for note in planned)
                report["planned"] += len(planned)
                suffix = f" (sync pass {pass_index + 1})" if pass_index else ""
                print(f"Active={len(live)} changed/new/missing={len(planned)}{suffix}",
                      flush=True)
                for note in planned:
                    outcome = _export_note(state, work, note, dry_run, report)
                    if outcome in ("ok", "reused", "empty"):
                        # A note deferred by a mid-run source change is not a
                        # final failure once a later pass publishes its current
                        # signature; the stale deferral must not force exit 2.
                        _clear_deferred_failure(report, note["uuid"])
                # Re-read the authoritative VM snapshot: guest sync may have
                # changed or added notes while this batch was rendering. A
                # failed recheck is recorded below and forces a nonzero exit.
                try:
                    latest = inventory(work, upload_helper=False)
                except Exception as error:
                    # Never report success when the authoritative snapshot
                    # could not be refreshed: the active set is now unknown.
                    recheck_failed = True
                    recheck_error = str(error)[:300]
                    print(f"WARNING: authoritative inventory recheck failed: {error}",
                          flush=True)
                    break
                live = latest
                if not _unreconciled(live, processed):
                    break
                sync_passes += 1
            # The last successfully read snapshot is authoritative; the
            # original count is preserved separately for audits.
            authoritative_count = len(live)
            # A failed recheck is recorded in inventory_recheck_error and forces
            # a nonzero exit; it does not inflate the pending list with UUIDs
            # whose authoritative state was never observed.
            if limit:
                # Never-picked notes are intentionally deferred by the caller,
                # but an already-processed note that changed mid-run is a real
                # reconciliation failure and must still be reported.
                pending_sync = [u for u in _unreconciled(live, processed)
                                if u in processed]
            else:
                pending_sync = _unreconciled(live, processed)
            report["active"] = authoritative_count
            report["authoritative_active"] = authoritative_count
            report["sync_passes"] = sync_passes
            report["inventory_recheck_failed"] = recheck_failed
            report["inventory_recheck_error"] = recheck_error
            report["pending_sync"] = pending_sync
            # Empty notes cached by a previous run are still first-class corpus
            # outcomes; include them in the final parity report rather than
            # making the absence of an export indistinguishable from omission.
            report["verified_empty"] = _verified_empty_records(state, live)
            if not dry_run:
                _write_live_snapshot(live)
            if uuids:
                missing = set(uuids) - planned_uuids
                if missing:
                    raise ValueError(
                        "Requested UUIDs are not currently changed/new: "
                        + ",".join(sorted(missing))
                    )
            report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not dry_run:
                out = HIST / "incremental_report.json"
                part = out.with_suffix(".tmp")
                part.write_text(json.dumps(report, indent=2))
                os.replace(part, out)
            state.close()
            print(
                f"Updated={len(report['updated'])} verified_empty={len(report['verified_empty'])} "
                f"failed={len(report['failed'])} pending_sync={len(pending_sync)}",
                flush=True,
            )
            return 2 if (report["failed"] or pending_sync or recheck_failed) else 0


def text_chars(path: Path) -> int:
    """Total extractable characters (recall-engine readability probe)."""
    from pypdf import PdfReader
    r = PdfReader(str(path))
    return sum(len(p.extract_text() or "") for p in r.pages)


def ocr_env() -> dict:
    """Route OCR imports only through the consolidated GPU and CPU libraries."""
    env = dict(os.environ)
    shared_root = PROJECT_ROOT.parent
    gpu_site = shared_root / "python_libs_gpu/site-packages"
    cpu_site = shared_root / "python_libs/site-packages"
    libs = [str(gpu_site), str(cpu_site)]
    have = [p for p in libs if Path(p).is_dir()]
    if have:
        env["PYTHONPATH"] = os.pathsep.join(have)
    env["PYTHONNOUSERSITE"] = "1"
    have_ld = [str(path) for root in (gpu_site, cpu_site)
               for pattern in ("nvidia/*/lib", "*.libs")
               for path in root.glob(pattern) if path.is_dir()]
    if have_ld:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(have_ld + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return env


def postprocess(report_path: Path | None = None) -> int:
    """Audit canonical compressed rows only; never derive a raw source here.

    A legacy/non-canonical row is reported pending and must be re-exported from
    the VM by the exporter, which can restore links the old bytes never had.
    """
    state = open_state()
    audited = pending = failed = 0
    rows = [dict(zip(("uuid", "path", "pages", "derived_chars"), value))
            for value in state.execute(
                "SELECT uuid,path,pages,derived_chars FROM exports")]
    for row in rows:
        u, path = row["uuid"], row["path"]
        source = Path(path) if path else None
        if source and _under(source, DERIVED):
            # Already the final compressed output: record/verify it, never OCR.
            if output_valid(source, row["pages"]):
                sha = digest(source)
                chars = row["derived_chars"] if row["derived_chars"] is not None else text_chars(source)
                state.execute(
                    "UPDATE exports SET derived_path=?,derived_sha256=?,derived_chars=? WHERE uuid=?",
                    (str(source), sha, chars, u))
                audited += 1
            else:
                state.execute(
                    "UPDATE exports SET derived_path=NULL,derived_sha256=NULL,derived_chars=NULL WHERE uuid=?",
                    (u,))
                failed += 1
                print(f"DERIVED_INVALID {u}: canonical output failed validation", flush=True)
            continue
        # Missing, external or legacy rows are never treated as raw input.
        pending += 1
        print(f"DERIVED_PENDING {u}: no canonical compressed output; VM re-export required",
              flush=True)
    state.commit()
    refresh_mirror(state)
    state.close()
    print(f"postprocess: audited={audited} pending={pending} failed={failed}", flush=True)
    return 2 if (failed or pending) else 0


def refresh_mirror(state: sqlite3.Connection | None = None) -> int:
    """Deprecated no-op: Recall indexes the canonical PDF tree directly.

    Retain the callable for older CLI/tests, but never recreate the redundant
    `notes_mirror/` symlink farm or its stale manifest.
    """
    del state
    print("mirror: disabled (Recall indexes canonical outputs)", flush=True)
    return 0


def build_manifest() -> dict:
    """Manifest of final, independently verified compressed outputs only."""
    state = open_state()
    columns = [item[0] for item in state.execute("SELECT * FROM exports LIMIT 0").description]
    items, total = [], 0
    for row in state.execute("SELECT * FROM exports").fetchall():
        data = dict(zip(columns, row))
        q = Path(data["path"]) if data.get("path") else None
        # An external path must never be inspected or reported as valid.
        ok = bool(q) and _under(q, DERIVED) and output_valid(q, data.get("pages"))
        if ok:
            total += q.stat().st_size
        items.append({"uuid": data["uuid"], "title": data["title"], "folder": data["folder"],
                      "modified": data["modified"], "path": str(q) if q else None,
                      "sha256": data.get("sha256"), "derived_sha256": data.get("derived_sha256"),
                      "original_sha256": data.get("original_sha256"),
                      "renderer": data.get("renderer"), "pages": data.get("pages"),
                      "ocr": data.get("ocr", 0), "chars": data.get("derived_chars"),
                      "valid": ok})
    state.close()
    valid = sum(1 for item in items if item["valid"])
    manifest = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total_notes": len(items), "exported": valid,
                "total_bytes": total, "invalid_pdfs": len(items) - valid,
                "notes": items}
    out = ROOT / "notes/export_manifest.json"
    part = out.with_suffix(".tmp")
    part.write_text(json.dumps(manifest, indent=1))
    os.replace(part, out)
    print(f"manifest: {valid}/{len(items)} valid", flush=True)
    return manifest


def repair(apply: bool = False) -> int:
    """Re-point rows to verified compressed outputs and prune only known duplicates.

    The recorded row is the confirmed inventory.  A duplicate is removed only
    when both copies are real (never a symlink), structurally valid, carry the
    same digest, and one copy is exactly the canonical target for the row.
    Anything less is reported as a conflict instead of being deleted.
    """
    state = open_state()
    actions = {"repointed": [], "pending": [], "duplicates_removed": [],
               "conflicts": [], "broken_links_removed": [], "unmapped": []}
    if DERIVED.is_dir():
        for path in sorted(DERIVED.rglob("*")):
            if path.is_symlink() and not path.exists() and UUID_NAME.search(path.name):
                actions["broken_links_removed"].append(str(path))
                if apply:
                    try:
                        path.unlink()
                    except OSError:
                        pass
    columns = [item[0] for item in state.execute("SELECT * FROM exports LIMIT 0").description]
    for row in state.execute("SELECT * FROM exports").fetchall():
        data = dict(zip(columns, row))
        u = data["uuid"]
        note = {"uuid": u, "title": data["title"], "folder": data["folder"],
                "modified": data["modified"]}
        target = target_for(note)
        candidates, seen = [], set()

        def add_uuid_candidate(value: str | Path, require_suffix: bool) -> None:
            """Containment is checked before any file is opened.

            A path recorded on the row itself is a proven mapping, so a legacy
            unsuffixed name is adopted there; files found only by scanning are
            adopted solely when they carry this UUID in their name.
            """
            path = Path(value)
            if path.is_symlink() or not _under(path, DERIVED):
                return
            if require_suffix and not path.name.endswith(f"__{u}.pdf"):
                return
            if not output_valid(path):
                return
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                candidates.append(path)

        for value in (data.get("path"), data.get("derived_path")):
            if value:
                add_uuid_candidate(value, require_suffix=False)
        for path in sorted(DERIVED.rglob(f"*__{u}.pdf")):
            add_uuid_candidate(path, require_suffix=True)
        # Ambiguous unsuffixed outputs are only reported, never moved/deleted.
        unsuffixed, unsuffixed_seen = [], set()
        for value in (data.get("path"), data.get("derived_path")):
            if not value:
                continue
            path = Path(value)
            if path.name.endswith(f"__{u}.pdf") or path.is_symlink() or not _under(path, DERIVED):
                continue
            key = str(path.resolve())
            if key in seen or key in unsuffixed_seen:
                continue
            if output_valid(path):
                unsuffixed_seen.add(key)
                unsuffixed.append(path)
        if not candidates:
            if unsuffixed:
                actions["unmapped"].append({
                    "uuid": u, "paths": [str(path) for path in unsuffixed],
                    "reason": "ambiguous unsuffixed output; VM re-export required"})
            actions["pending"].append({"uuid": u, "title": data["title"], "folder": data["folder"]})
            if apply:
                state.execute(
                    "UPDATE exports SET path=?,derived_path=NULL,derived_sha256=NULL,"
                    "derived_chars=NULL WHERE uuid=?", (str(target), u))
            continue
        canonical = target if target in candidates else candidates[0]
        others = [path for path in candidates if path != canonical]
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            if canonical != target:
                os.replace(canonical, target)
                canonical = target
        if apply:
            pages = verify(canonical)
            sha = digest(canonical)
            chars = data.get("derived_chars") or text_chars(canonical)
            # Repair only re-points files: it must not claim the new structure
            # revision, because the existing bytes may still lack links.
            state.execute(
                "UPDATE exports SET title=?,folder=?,modified=?,path=?,sha256=?,pages=?,"
                "original_sha256=COALESCE(original_sha256,?),derived_path=?,derived_sha256=?,"
                "derived_chars=? WHERE uuid=?",
                (note["title"], note["folder"], note["modified"], str(canonical), sha,
                 pages, data.get("original_sha256") or data.get("sha256"), str(canonical),
                 sha, chars, u))
        actions["repointed"].append({"uuid": u, "path": str(target)})
        canonical_sha = digest(canonical)
        for other in unsuffixed:
            try:
                same = digest(other) == canonical_sha
            except OSError:
                continue
            if same:
                actions["unmapped"].append({
                    "uuid": u, "alias": str(other), "hash_matches": True,
                    "reason": "unsuffixed alias kept for manual proof"})
            else:
                actions["conflicts"].append({"uuid": u, "keep": str(canonical),
                                             "differing": str(other)})
        for other in others:
            if not known_uuid_path(other, u):
                continue
            try:
                other_sha = digest(other)
            except OSError:
                continue
            if other_sha != canonical_sha:
                actions["conflicts"].append({"uuid": u, "keep": str(canonical),
                                             "differing": str(other)})
                continue
            actions["duplicates_removed"].append(str(other))
            if apply:
                try:
                    other.unlink()
                except OSError:
                    pass
                _prune_empty_dirs(other.parent)
    if apply:
        state.commit()
        refresh_mirror(state)
    else:
        state.rollback()
    state.close()
    canvas = {"note": "heuristic only; no file is treated as defective or deleted"}
    if CANVAS_AUDIT.exists():
        try:
            audit = json.loads(CANVAS_AUDIT.read_text()).get("audit", {})
            canvas.update({key: audit.get(key) for key in
                           ("files", "black_files", "black_pages", "symlinks",
                            "symlink_defects", "pages_inspected")})
        except Exception as error:
            canvas["error"] = str(error)[:120]
    report = {"applied": apply, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "repointed": len(actions["repointed"]), "pending": len(actions["pending"]),
              "duplicates_removed": len(actions["duplicates_removed"]),
              "conflicts": len(actions["conflicts"]),
              "broken_links_removed": len(actions["broken_links_removed"]),
              "unmapped": len(actions["unmapped"]),
              "canvas_audit_unproven": canvas, "detail": actions}
    out = HIST / "incremental_repair_report.json"
    part = out.with_suffix(".tmp")
    part.write_text(json.dumps(report, indent=2))
    os.replace(part, out)
    print(
        f"repair({'apply' if apply else 'dry-run'}): repointed={len(actions['repointed'])} "
        f"pending={len(actions['pending'])} duplicates_removed={len(actions['duplicates_removed'])} "
        f"conflicts={len(actions['conflicts'])} broken_links_removed={len(actions['broken_links_removed'])} "
        f"unmapped={len(actions['unmapped'])}",
        flush=True)
    return 2 if actions["conflicts"] else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--uuid", action="append", dest="uuids",
                    help="only export this changed/new note UUID; repeatable for samples")
    ap.add_argument("--postprocess", action="store_true",
                    help="audit canonical compressed outputs only; legacy rows stay pending")
    ap.add_argument("--manifest", action="store_true",
                    help="rebuild merged manifest from verified canonical compressed outputs")
    ap.add_argument("--mirror", action="store_true",
                    help="refresh interview symlink mirror only")
    ap.add_argument("--repair", action="store_true",
                    help="re-point state to verified compressed outputs; prune known duplicates")
    ap.add_argument("--apply", action="store_true",
                    help="with --repair: perform the re-point/prune instead of a dry run")
    args = ap.parse_args()
    if args.repair:
        raise SystemExit(repair(args.apply))
    if args.postprocess:
        raise SystemExit(postprocess())
    if args.manifest:
        manifest = build_manifest()
        raise SystemExit(2 if manifest["invalid_pdfs"] else 0)
    if args.mirror:
        raise SystemExit(0 if refresh_mirror() else 1)
    raise SystemExit(run(args.limit, args.dry_run, args.uuids))
