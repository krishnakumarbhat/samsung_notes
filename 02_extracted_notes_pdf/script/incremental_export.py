"""Read-only incremental Samsung Notes export with bounded page chunks.

Uses the independently built sdocx-cli on Linux. Existing verified exports
are preserved until an entire replacement passes validation. Unsupported
rendering is a reported failure, never a successful blank export.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT / "02_extracted_notes_pdf"
HIST = ROOT / "ai_agend_history"
DEST = ROOT / "notes/original"
DERIVED = ROOT / "notes/compressed"
STATE = HIST / "incremental_state.sqlite"
TRACKER = HIST / "export_tracker.sqlite"
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


def transport(kind: str) -> list[str]:
    """Resolve guest credentials from environment or the private .env file."""
    password = os.environ.get("SAMSUNG_NOTES_PASSWORD")
    env = ROOT / ".env"
    if not password and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SAMSUNG_NOTES_PASSWORD="):
                password = line.partition("=")[2].strip().strip("\"'")
    if not password:
        raise RuntimeError("Set SAMSUNG_NOTES_PASSWORD in samsung_notes/.env")
    # sshpass reads an environment variable, avoiding a password in argv.
    os.environ["SSHPASS"] = password
    base = ["sshpass", "-e", kind, "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15"]
    return base + (["-p", "2222", "notes@127.0.0.1"] if kind == "ssh"
                   else ["-O", "-P", "2222"])


def upload(path: Path, remote: str) -> None:
    """Transfer a helper without titles or other note data in shell arguments."""
    execute(transport("scp") + [str(path), f"notes@127.0.0.1:{remote}"])


def inventory(work: Path) -> dict:
    """Query production SQLite strictly read-only; include full folder ancestry."""
    helper = work / "inventory.py"
    helper.write_text('''import json,sqlite3
from pathlib import Path
p=Path.home()/"AppData/Local/Packages/SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState/Storage.sqlite"
c=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True)
c.execute("PRAGMA query_only=ON")
cats={r[0]:(r[1],r[2]) for r in c.execute("SELECT UUID,ParentUUID,DisplayName FROM CategoryTreeDB WHERE IsDeleted=0")}
items=[]
for u,t,k,m in c.execute("SELECT UUID,Title,CategoryUUID,LastModifiedAt FROM NoteDB WHERE DeletedStatus=0"):
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
    upload(helper, remote)
    out = execute(transport("ssh") + [f"C:\\py\\python.exe {remote}"], 120)
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


def verify(path: Path, expected: int | None = None) -> int:
    """Decode the PDF and check page count, syntax and EOF, not magic alone."""
    import pikepdf
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("bad PDF header")
        f.seek(max(0, path.stat().st_size - 2048))
        if b"%%EOF" not in f.read():
            raise ValueError("incomplete PDF")
    with pikepdf.open(path) as pdf:
        n = len(pdf.pages)
        problems = pdf.check_pdf_syntax()
        errors = [p for p in problems if "error" in str(p).lower()]
        if not n or errors or (expected is not None and n != expected):
            raise ValueError(f"PDF validation: pages={n}, expected={expected}, errors={errors or problems}")
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
        execute(["pdftoppm", "-r", "36", "-png", "-f", str(p), "-l", str(p),
                 str(path), str(stem)], 120)
        # pdftoppm appends its own page suffix; accept whatever it wrote.
        found = sorted(path.parent.glob("_check-*.png"))
        if not found:
            raise ValueError("could not rasterize sample page")
        png = found[0]
        try:
            with Image.open(png).convert("RGB") as im:
                small = im.resize((180, 240))
                raw = small.tobytes()
                px = [raw[i:i + 3] for i in range(0, len(raw), 3)]
            bg = max(set(px), key=px.count)
            varied = sum(1 for v in px
                         if abs(v[0] - bg[0]) + abs(v[1] - bg[1])
                         + abs(v[2] - bg[2]) > 36)
            if varied >= 20:
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
        from page_delta import _note_layout_sensitive
        if _note_layout_sensitive(archive) is not True:
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


def open_state() -> sqlite3.Connection:
    """Seed recorded export epochs once; never refresh them merely by indexing."""
    c = sqlite3.connect(STATE)
    c.execute("CREATE TABLE IF NOT EXISTS exports(uuid TEXT PRIMARY KEY, modified INTEGER, title TEXT, folder TEXT, path TEXT, sha256 TEXT, renderer TEXT, pages INTEGER)")
    c.execute("""CREATE TABLE IF NOT EXISTS empty_sources(
        uuid TEXT PRIMARY KEY, modified INTEGER NOT NULL, title TEXT NOT NULL,
        folder TEXT NOT NULL, visible_pages INTEGER NOT NULL,
        checked_at TEXT NOT NULL, reason TEXT NOT NULL
    )""")
    if TRACKER.exists():
        with sqlite3.connect(f"file:{TRACKER}?mode=ro", uri=True) as old:
            for u,m,t,f,p,h in old.execute("SELECT uuid,last_modified,title,folder_path,host_file_path,sha256 FROM notes WHERE export_status='SUCCESS'"):
                path = Path(p or "")
                if not path.is_absolute():
                    path = ROOT / path
                if path.is_file():
                    c.execute("INSERT OR IGNORE INTO exports(uuid,modified,title,folder,path,sha256,renderer,pages) VALUES(?,?,?,?,?,?,?,?)",
                              (u,m,t,f,str(path),h,"native-existing",None))
    c.commit()
    return c


def run(limit: int | None, dry_run: bool, uuids: list[str] | None = None) -> int:
    """Copy changed notes only; publish after complete validation and epoch check."""
    HIST.mkdir(exist_ok=True)
    with (HIST / "incremental.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = open_state()
        work_root = Path(os.environ.get("SN_EXPORT_WORK", str(HIST)))
        work_root.mkdir(parents=True, exist_ok=True)
        # The scratch tree may live outside the capped worktree; the guard then
        # only has to respect the volume free-space floor.
        scratch_inside_workspace = work_root.resolve().is_relative_to(PROJECT_ROOT.resolve())
        with tempfile.TemporaryDirectory(prefix="sn-incremental-", dir=work_root) as tmp:
            work = Path(tmp)
            live = inventory(work)
            planned = []
            for u, note in live.items():
                empty = state.execute(
                    "SELECT modified,title,folder FROM empty_sources WHERE uuid=?", (u,)
                ).fetchone()
                if empty and empty == (note["modified"], note["title"], note["folder"]):
                    continue
                if empty:
                    state.execute("DELETE FROM empty_sources WHERE uuid=?", (u,))
                row = state.execute("SELECT modified,title,folder,path FROM exports WHERE uuid=?", (u,)).fetchone()
                if row and row[:3] == (note["modified"],note["title"],note["folder"]) and Path(row[3]).is_file():
                    continue
                planned.append(note)
            if uuids:
                requested = set(uuids)
                present = {note["uuid"] for note in planned}
                missing = requested - present
                if missing:
                    raise ValueError(
                        "Requested UUIDs are not currently changed/new: "
                        + ",".join(sorted(missing))
                    )
                planned = [note for note in planned if note["uuid"] in requested]
            if limit:
                planned = planned[:limit]
            report = {"active":len(live), "planned":len(planned), "updated":[], "failed":[],
                      "skipped_empty":[], "dry_run":dry_run}
            print(f"Active={len(live)} changed/new/missing={len(planned)}", flush=True)
            for note in planned:
                u = note["uuid"]
                print(f"EXPORT {note['folder']}/{note['title']} ({u})", flush=True)
                if dry_run:
                    continue
                try:
                    from derive_corpus import guard
                    # Snapshot + archive + raw/derived candidates require transient
                    # headroom. Refuse before transfer rather than exceed the cap.
                    guard(int(note.get("payload_bytes", 0)) * 4 + 16 * 1024**2,
                          workspace_bound=scratch_inside_workspace)
                    # UUID input validation prevents remote shell interpolation.
                    if not re.fullmatch(r"[0-9a-fA-F-]{36}", u):
                        raise ValueError("invalid UUID")
                    before = inventory(work).get(u)
                    if before != note:
                        raise RuntimeError("source changed before snapshot; deferred")
                    note_work = work / u
                    note_work.mkdir()
                    remote = f"notes@127.0.0.1:{GUEST}/wdoc/{u}"
                    execute(transport("scp") + ["-r", remote, str(note_work)], 600)
                    from page_delta import compile_note
                    from derive_corpus import page_ocr_hook
                    output, pages, warnings = compile_note(note_work / u, note_work, searchable_hook=page_ocr_hook)
                    after = inventory(work).get(u)
                    if after != before:
                        raise RuntimeError("source changed during export; deferred, old PDF retained")
                    previous = state.execute("SELECT path FROM exports WHERE uuid=?", (u,)).fetchone()
                    if previous and Path(previous[0]).is_file() and note["title"] == state.execute("SELECT title FROM exports WHERE uuid=?",(u,)).fetchone()[0] and note["folder"] == state.execute("SELECT folder FROM exports WHERE uuid=?",(u,)).fetchone()[0]:
                        target = Path(previous[0])
                    else:
                        folder = [safe_component(p) for p in note["folder"].split("/") if p] or ["Uncategorized"]
                        target = DEST.joinpath(*folder, safe_component(note["title"])+f"__{u}.pdf")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    part = target.with_name(target.name + ".part")
                    shutil.copyfile(output, part)
                    verify(part, pages)
                    sha = digest(part)
                    # Preserve the exact previous export; never discard a raw PDF.
                    if target.is_file() and digest(target) != sha:
                        preserved = HIST / "preserved_originals"
                        preserved.mkdir(exist_ok=True)
                        backup = preserved / f"{u}.{digest(target)}.pdf"
                        if not backup.exists():
                            os.link(target, backup)
                    with part.open("rb") as f:
                        os.fsync(f.fileno())
                    os.replace(part, target)
                    derived = DERIVED / target.relative_to(DEST)
                    derived.parent.mkdir(parents=True, exist_ok=True)
                    cached_derived = note_work / "page-delta-searchable.pdf"
                    if cached_derived.is_file():
                        from derive_corpus import verify_pair
                        verify_pair(target, cached_derived)
                        derived_part = derived.with_name(derived.name + ".part")
                        shutil.copyfile(cached_derived, derived_part)
                        os.replace(derived_part, derived)
                    elif derived.is_symlink():
                        derived.unlink()
                        derived.symlink_to(os.path.relpath(target, derived.parent))
                    elif not derived.exists():
                        derived.symlink_to(os.path.relpath(target, derived.parent))
                    state.execute(
                        """INSERT OR REPLACE INTO exports
                        (uuid,modified,title,folder,path,sha256,renderer,pages)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (u,note["modified"],note["title"],note["folder"],str(target),sha,"sdocx-best-effort",pages),
                    )
                    state.execute("DELETE FROM empty_sources WHERE uuid=?", (u,))
                    state.commit()
                    report["updated"].append({**note,"path":str(target),"pages":pages,"sha256":sha,
                                              "warnings":warnings,"derived_from_page_cache":cached_derived.is_file()})
                except Exception as e:
                    archive = work / u / "source.sdocx"
                    source = work / u / u
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
                            print(f"SKIPPED_EMPTY {u} visible_pages={visible_pages}; prior export retained", flush=True)
                            continue
                    report["failed"].append({**note,"reason":str(e)})
                    print(f"FAILED {note['title']}: {str(e)[:300]}", flush=True)
            report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not dry_run:
                out = HIST / "incremental_report.json"
                part = out.with_suffix(".tmp")
                part.write_text(json.dumps(report, indent=2))
                os.replace(part, out)
            state.close()
            print(
                f"Updated={len(report['updated'])} skipped_empty={len(report['skipped_empty'])} "
                f"failed={len(report['failed'])}",
                flush=True,
            )
            return 2 if report["failed"] else 0


def text_chars(path: Path) -> int:
    """Total extractable characters (recall-engine readability probe)."""
    from pypdf import PdfReader
    r = PdfReader(str(path))
    return sum(len(p.extract_text() or "") for p in r.pages)


def ocr_env() -> dict:
    """Runtime routing for the OCR stack (consolidated under sam_win/).

    Put the shared GPU Paddle runtime before the OCR package tree (which also
    contains CPU Paddle). Include wheel-bundled native libraries before import.
    This routes sys.path/LD only; it does not install or mutate packages.
    """
    env = dict(os.environ)
    shared_root = PROJECT_ROOT.parent
    gpu_site = shared_root / "python_libs_gpu/site-packages"
    ocr_site = PROJECT_ROOT / "05_environments_and_venvs/python_ocr_env/site-packages"
    cpu_site = shared_root / "python_libs/site-packages"
    libs = [str(gpu_site), str(ocr_site), str(cpu_site)]
    libs.extend(str(path) for path in sorted(Path.home().glob(".local/lib/python*/site-packages")))
    libs.extend(filter(None, os.environ.get("SAM_EXTRA_SITE_PACKAGES", "").split(os.pathsep)))
    have = [p for p in libs if Path(p).is_dir()]
    if have:
        env["PYTHONPATH"] = os.pathsep.join(have + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.setdefault("SAM_PDF_PYTHON_LIB_ROOTS", os.pathsep.join(have))
    cuda_candidates = [
        gpu_site / "nvidia/cuda_runtime/lib",
        gpu_site / "nvidia/cuda_nvrtc/lib",
        gpu_site / "nvidia/cublas/lib",
        gpu_site / "nvidia/cudnn/lib",
        cpu_site / "nvidia/cudnn/lib",
        ocr_site / "nvidia/cu13/lib",
        ocr_site / "opencv_python.libs",
        ocr_site / "opencv_python_headless.libs",
        ocr_site / "numpy.libs",
        ocr_site / "scipy.libs",
        ocr_site / "av.libs",
    ]
    cuda_candidates.extend(sorted(ocr_site.glob("*.libs")))
    have_ld = [str(path) for path in cuda_candidates if path.is_dir()]
    if have_ld:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(have_ld + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return env


def postprocess(report_path: Path | None = None) -> int:
    """Publish derived searchable PDFs only; never OCR-replace a raw original."""
    from derive_corpus import derive_file
    report_path = report_path or (HIST / "incremental_report.json")
    report = json.loads(report_path.read_text())
    state = open_state()
    columns = {r[1] for r in state.execute("PRAGMA table_info(exports)")}
    for name, kind in (("derived_path", "TEXT"), ("derived_sha256", "TEXT"), ("derived_chars", "INTEGER")):
        if name not in columns:
            state.execute(f"ALTER TABLE exports ADD COLUMN {name} {kind}")
    state.commit()
    done = failed = 0
    for note in report.get("updated", []):
        row = state.execute("SELECT path FROM exports WHERE uuid=?", (note["uuid"],)).fetchone()
        if not row:
            continue
        source = Path(row[0])
        try:
            if note.get("derived_from_page_cache"):
                done += 1
                continue
            result = derive_file(source, ocr=True)
            if result["status"] not in ("searchable", "no_text"):
                raise RuntimeError(result["status"])
            destination = DERIVED / source.relative_to(DEST)
            state.execute("UPDATE exports SET derived_path=?,derived_sha256=?,derived_chars=? WHERE uuid=?",
                          (str(destination), result["sha256"], result["chars"], note["uuid"]))
            state.commit()
            done += 1
        except Exception as error:
            failed += 1
            print(f"DERIVED_FAILED {note['uuid']} {type(error).__name__}: {str(error)[:150]}", flush=True)
    refresh_mirror(state)
    state.close()
    print(f"postprocess: derived={done} failed={failed}", flush=True)
    return 2 if failed else 0


def refresh_mirror(state: sqlite3.Connection | None = None) -> int:
    """Symlink mirror + manifest for the recall engine (no PDF duplication)."""
    MIR = PROJECT_ROOT / "04_learning_recall_app/notes_mirror"
    MIR.mkdir(parents=True, exist_ok=True)
    own = state is None
    if own:
        state = open_state()
    man, n = {}, 0
    for u, p, h, r, pg in state.execute("SELECT uuid,path,sha256,renderer,pages FROM exports"):
        raw = Path(p)
        q = DERIVED / raw.relative_to(DEST)
        if not q.exists():
            q = raw
        if q.is_file() and q.stat().st_size > 5000:
            link = MIR / f"{u}.pdf"
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(q)
            except OSError as e:
                print(f"LINKFAIL {u}: {e}", flush=True)
                continue
            cols = [d[0] for d in state.execute("SELECT * FROM exports LIMIT 0").description]
            row = state.execute("SELECT * FROM exports WHERE uuid=?", (u,)).fetchone()
            d = dict(zip(cols, row))
            man[u] = {"path": str(link), "target": str(q), "sha256": h,
                      "renderer": r, "pages": pg, "ocr": d.get("ocr", 0),
                      "chars": d.get("chars")}
            n += 1
    (MIR / "manifest.json").write_text(json.dumps({"count": n, "notes": man}, indent=1))
    if own:
        state.close()
    print(f"mirror: {n} links", flush=True)
    return n


def build_manifest() -> dict:
    """Merged manifest from state + verified files (tracker-independent)."""
    state = open_state()
    items, total = [], 0
    for u, m, t, f, p, h, r, pg in state.execute(
            "SELECT uuid,modified,title,folder,path,sha256,renderer,pages FROM exports"):
        q = Path(p)
        ok = q.is_file()
        if ok:
            try:
                verify(q)
                total += q.stat().st_size
            except Exception:
                ok = False
        cols = [d[0] for d in state.execute("SELECT * FROM exports LIMIT 0").description]
        row = state.execute("SELECT * FROM exports WHERE uuid=?", (u,)).fetchone()
        d = dict(zip(cols, row))
        items.append({"uuid": u, "title": t, "folder": f, "modified": m,
                      "path": str(q), "sha256": h, "renderer": r, "pages": pg,
                      "ocr": d.get("ocr", 0), "valid": ok})
    state.close()
    valid = sum(1 for i in items if i["valid"])
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--uuid", action="append", dest="uuids",
                    help="only export this changed/new note UUID; repeatable for samples")
    ap.add_argument("--postprocess", action="store_true",
                    help="selective OCR sandwich on newly exported PDFs + mirror refresh")
    ap.add_argument("--manifest", action="store_true",
                    help="rebuild merged manifest from state + file verification")
    ap.add_argument("--mirror", action="store_true",
                    help="refresh interview symlink mirror only")
    args = ap.parse_args()
    if args.postprocess:
        raise SystemExit(postprocess())
    if args.manifest:
        build_manifest()
        raise SystemExit(0)
    if args.mirror:
        raise SystemExit(0 if refresh_mirror() else 1)
    raise SystemExit(run(args.limit, args.dry_run, args.uuids))
