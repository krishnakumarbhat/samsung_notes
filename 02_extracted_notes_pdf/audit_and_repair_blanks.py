"""Audit every exported PDF and optionally repair only verified ink false-passes.

Storage.sqlite is accessed by a guest helper opened in SQLite read-only mode with
query_only enabled. The default command audits only. ``--repair`` snapshots the
matching WDoc folder and changes only a locally exported PDF plus its local state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
NOTES = ROOT / "notes/original"
HISTORY = ROOT / "ai_agend_history"
STATE_DB = HISTORY / "incremental_state.sqlite"
MIN_PDF_BYTES = 10 * 1024
RASTER_SCALE = 0.5

SCRIPT_DIR = PROJECT_ROOT / "03_rtx_compression_ocr"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPT_DIR))
from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

import pikepdf  # noqa: E402
from pymupdf_compat import fitz  # noqa: E402
from script import incremental_export as ie  # noqa: E402


def read_state_rows() -> list[dict]:
    """Read local incremental export state in query-only SQLite mode."""
    connection = sqlite3.connect(STATE_DB.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(exports)")]
        return [dict(zip(columns, row)) for row in connection.execute("SELECT * FROM exports")]
    finally:
        connection.close()


def read_wdoc_inventory() -> dict[str, dict]:
    """Read metadata and WDoc file sizes through the guest's read-only helper."""
    with tempfile.TemporaryDirectory(prefix="wdoc-audit-") as temporary:
        local_helper = Path(temporary) / "wdoc_inventory_ro.py"
        local_helper.write_bytes((ROOT / "script/wdoc_inventory_ro.py").read_bytes())
        remote_helper = "C:/Users/notes/wdoc_inventory_ro.py"
        ie.upload(local_helper, remote_helper)
        try:
            output = ie.execute(
                ie.transport("ssh")
                + [r"C:\py\python.exe C:\Users\notes\wdoc_inventory_ro.py"],
                timeout=300,
            )
        finally:
            try:
                ie.execute(
                    ie.transport("ssh") + [r"cmd /c del C:\Users\notes\wdoc_inventory_ro.py"],
                    timeout=30,
                )
            except Exception:
                pass
    payload = next((line for line in output.splitlines() if line.startswith("[")), None)
    if payload is None:
        raise RuntimeError("Guest did not return the read-only WDoc inventory JSON.")
    return {row["uuid"]: row for row in json.loads(payload)}


def index_exports(rows: list[dict]) -> dict[str, dict]:
    """Index export rows by resolved PDF path."""
    output = {}
    for row in rows:
        if row.get("path"):
            output[str(Path(row["path"]).resolve())] = row
    return output


def match_wdoc(path: Path, state_row: dict | None, inventory: dict[str, dict]) -> dict | None:
    """Match by tracked UUID, filename UUID suffix, or unique folder/title pair."""
    if state_row and state_row.get("uuid") in inventory:
        return inventory[state_row["uuid"]]
    for uuid, row in inventory.items():
        if path.stem.endswith(f"__{uuid}"):
            return row
    try:
        folder = path.parent.relative_to(NOTES).as_posix() or "Uncategorized"
    except ValueError:
        return None
    title = ie.safe_component(path.stem)
    matches = [
        row for row in inventory.values()
        if (row["folder"] or "Uncategorized") == folder
        and ie.safe_component(row["title"]) == title
    ]
    return matches[0] if len(matches) == 1 else None


def inspect_pdf(
    path: Path,
    expected_pages: int | None = None,
    wdoc_page_files: int | None = None,
    render_dpi: int = 36,
) -> dict:
    """Inspect PDF syntax, page counts, text density, and pure-white renders."""
    size = path.stat().st_size if path.is_file() else 0
    result = {
        "path": str(path),
        "bytes": size,
        "pdf_pages": 0,
        "expected_state_pages": expected_pages,
        "raw_wdoc_page_files": wdoc_page_files,
        "raw_wdoc_count_differs": False,
        "page_text_chars": [],
        "blank_pages": [],
        "flags": [],
    }
    if not path.is_file():
        result["flags"].append("missing")
        return result
    if size < MIN_PDF_BYTES:
        result["flags"].append("file-under-10KiB")
    try:
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                result["flags"].append("bad-header")
                return result
            source.seek(max(0, size - 2048))
            if b"%%EOF" not in source.read():
                result["flags"].append("missing-eof")
        with pikepdf.Pdf.open(path) as parsed:
            result["pdf_pages"] = len(parsed.pages)
            errors = [
                str(problem) for problem in parsed.check_pdf_syntax()
                if "error" in str(problem).lower()
            ]
            if errors:
                result["flags"].append("pdf-syntax-error")
            if not parsed.pages:
                result["flags"].append("zero-pages")
            if expected_pages and len(parsed.pages) != expected_pages:
                result["flags"].append("incremental-state-page-count-mismatch")
        with fitz.open(path) as document:
            if wdoc_page_files is not None:
                # Raw WDoc may retain hidden/deleted .page records; record this
                # diagnostic but do not mistake it for visible PDF page count.
                result["raw_wdoc_count_differs"] = len(document) != wdoc_page_files
            for number, page in enumerate(document, start=1):
                text = " ".join(page.get_text("text").split())
                result["page_text_chars"].append(len(text))
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(render_dpi / 72.0, render_dpi / 72.0),
                    colorspace=fitz.csGRAY,
                    alpha=False,
                )
                if not text and pixmap.samples and pixmap.samples.count(255) == len(pixmap.samples):
                    result["blank_pages"].append(number)
    except Exception as error:  # malformed PDFs should be reported, not terminate the audit
        result["flags"].append(f"unreadable:{type(error).__name__}")
    if result["blank_pages"]:
        result["flags"].append("white-blank-page-without-text")
    result["status"] = "flagged" if result["flags"] else "ok"
    return result


def package_wdoc(source: Path, destination: Path) -> None:
    """Create a temporary, uncompressed .sdocx ZIP from a read-only WDoc copy."""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def copy_wdoc(uuid: str, temporary: Path) -> Path:
    """Copy just one guest WDoc directory over SCP; never write to its source."""
    destination = temporary / uuid
    destination.mkdir(parents=True, exist_ok=True)
    remote = f"notes@127.0.0.1:{ie.GUEST}/wdoc/{uuid}"
    ie.execute(ie.transport("scp") + ["-r", remote, str(destination)], timeout=600)
    source = destination / uuid
    if not source.is_dir():
        raise RuntimeError(f"WDoc source unavailable for UUID {uuid}.")
    return source


def ink_hash(pdf: Path, stroke_pages: list[int]) -> str:
    """Hash low-resolution renders of source pages known to contain ink."""
    digest = hashlib.sha256()
    if not stroke_pages:
        raise ValueError("No source stroke pages are available for visual verification.")
    with fitz.open(pdf) as document:
        for page_number in stroke_pages:
            if not 1 <= page_number <= len(document):
                raise ValueError(f"Stroke page {page_number} is outside the rendered PDF.")
            pixmap = document[page_number - 1].get_pixmap(
                matrix=fitz.Matrix(RASTER_SCALE, RASTER_SCALE),
                colorspace=fitz.csGRAY,
                alpha=False,
            )
            pixels = pixmap.samples
            if not pixels or pixels.count(255) == len(pixels):
                raise ValueError(f"Source stroke page {page_number} rendered pure white.")
            digest.update(page_number.to_bytes(4, "big"))
            digest.update(pixels)
    return digest.hexdigest()


def backup_state() -> Path:
    """Create a consistent online backup of the local export state database."""
    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    destination = HISTORY / f"incremental_state.pre_blank_repair.{stamp}.sqlite"
    source_db = sqlite3.connect(STATE_DB)
    backup_db = sqlite3.connect(destination)
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()
    return destination


def ensure_ink_hash_column(connection: sqlite3.Connection) -> None:
    """Add the repair verification hash to the local export table if needed."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(exports)")}
    if "ink_sha256" not in columns:
        connection.execute("ALTER TABLE exports ADD COLUMN ink_sha256 TEXT")


def repair_candidate(finding: dict, source_record: dict, apply: bool) -> dict:
    """Re-render one confirmed ink false-pass; optionally publish with a backup."""
    uuid = source_record["uuid"]
    pdf_path = Path(finding["path"])
    original_sha = ie.digest(pdf_path)
    result = {"uuid": uuid, "status": "candidate"}
    with tempfile.TemporaryDirectory(prefix=f"blank-repair-{uuid[:8]}-") as temporary:
        work = Path(temporary)
        source = copy_wdoc(uuid, work)
        archive = work / "source.sdocx"
        package_wdoc(source, archive)
        visible_pages, page_info, probe_warnings = ie.probe_archive(archive)
        stroke_pages = [int(page["index"]) + 1 for page in page_info if int(page["strokes"]) > 0]
        result.update({
            "sdocx_visible_pages": visible_pages,
            "stroke_count": sum(int(page["strokes"]) for page in page_info),
            "stroke_pages": stroke_pages,
            "probe_warning_count": len(probe_warnings),
        })
        if not stroke_pages:
            result["status"] = "not-repairable-no-strokes"
            return result

        rendered, rendered_pages, render_warnings = ie.render(source, work, chunk=8)
        if rendered_pages != visible_pages:
            result["status"] = "manual-review-page-count-ambiguous"
            result["rendered_pages"] = rendered_pages
            return result
        ie.verify(rendered, rendered_pages)
        verified_ink_sha = ink_hash(rendered, stroke_pages)
        result.update({
            "rendered_pages": rendered_pages,
            "rendered_bytes": rendered.stat().st_size,
            "ink_sha256": verified_ink_sha,
            "render_warning_count": len(render_warnings),
        })
        if not apply:
            result["status"] = "verified-dry-run"
            return result
        if ie.digest(pdf_path) != original_sha:
            result["status"] = "deferred-source-changed"
            return result

        backup_dir = HISTORY / "repair_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        pdf_backup = backup_dir / f"{uuid}.{original_sha[:12]}.pre-repair.pdf"
        if not pdf_backup.exists():
            shutil.copy2(pdf_path, pdf_backup)
        state_backup = backup_state()
        replacement = pdf_path.with_name(pdf_path.name + ".repair.part")
        shutil.copyfile(rendered, replacement)
        ie.verify(replacement, rendered_pages)
        if ink_hash(replacement, stroke_pages) != verified_ink_sha:
            replacement.unlink(missing_ok=True)
            raise RuntimeError("Repair candidate failed its visible-ink hash check.")
        with replacement.open("rb") as stream:
            os.fsync(stream.fileno())

        connection = ie.open_state()
        try:
            ensure_ink_hash_column(connection)
            connection.commit()
            existing = connection.execute(
                "SELECT 1 FROM exports WHERE uuid=?", (uuid,)
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            new_sha = ie.digest(replacement)
            if existing:
                connection.execute(
                    "UPDATE exports SET path=?,sha256=?,renderer=?,pages=?,ink_sha256=? WHERE uuid=?",
                    (str(pdf_path), new_sha, "sdocx-verified-repair", rendered_pages, verified_ink_sha, uuid),
                )
            else:
                connection.execute(
                    """INSERT INTO exports
                    (uuid,modified,title,folder,path,sha256,renderer,pages,ink_sha256)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (uuid, int(source_record["modified"]), source_record["title"],
                     source_record["folder"], str(pdf_path), new_sha,
                     "sdocx-verified-repair", rendered_pages, verified_ink_sha),
                )
            os.replace(replacement, pdf_path)
            connection.commit()
        except Exception:
            connection.rollback()
            replacement.unlink(missing_ok=True)
            rollback_file = pdf_path.with_name(pdf_path.name + ".rollback.part")
            shutil.copy2(pdf_backup, rollback_file)
            os.replace(rollback_file, pdf_path)
            raise
        finally:
            connection.close()
        result.update({
            "status": "repaired",
            "pdf_backup": str(pdf_backup),
            "state_backup": str(state_backup),
            "sha256": ie.digest(pdf_path),
        })
    return result


def audit_all(apply_repairs: bool = False, render_dpi: int = 36) -> dict:
    """Scan all PDFs and repair only uniquely matched source-confirmed stroke pages."""
    state_rows = read_state_rows()
    exports = index_exports(state_rows)
    inventory = read_wdoc_inventory()
    pdf_paths = sorted(NOTES.rglob("*.pdf"))
    findings, repair_results, page_count_mismatches = [], [], []
    for path in pdf_paths:
        state_row = exports.get(str(path.resolve()))
        source_record = match_wdoc(path, state_row, inventory)
        finding = inspect_pdf(
            path,
            expected_pages=int(state_row["pages"]) if state_row and state_row.get("pages") else None,
            wdoc_page_files=len(source_record["page_files"]) if source_record else None,
            render_dpi=render_dpi,
        )
        if finding["raw_wdoc_count_differs"]:
            page_count_mismatches.append({
                "path": str(path),
                "uuid": source_record["uuid"] if source_record else None,
                "pdf_pages": finding["pdf_pages"],
                "raw_wdoc_page_files": finding["raw_wdoc_page_files"],
                "interpretation": "advisory: raw WDoc file count is not necessarily the visible page count",
            })
        if not finding["flags"]:
            continue
        finding["uuid"] = source_record["uuid"] if source_record else None
        finding["wdoc_match"] = source_record is not None
        findings.append(finding)
        actionable = (
            finding["bytes"] < MIN_PDF_BYTES
            or bool(finding["blank_pages"])
            or "incremental-state-page-count-mismatch" in finding["flags"]
            or any(flag.startswith("unreadable:") for flag in finding["flags"])
        )
        if apply_repairs and actionable and source_record:
            repair_results.append(repair_candidate(finding, source_record, apply=True))

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pdf_count": len(pdf_paths),
        "state_rows": len(state_rows),
        "flagged_count": len(findings),
        "unmatched_flagged_count": sum(not row["wdoc_match"] for row in findings),
        "raw_wdoc_page_count_diagnostics": len(page_count_mismatches),
        "page_count_mismatches": page_count_mismatches,
        "findings": findings,
        "repair_results": repair_results,
        "repair_mode": apply_repairs,
        "blank_render_dpi": render_dpi,
    }
    HISTORY.mkdir(parents=True, exist_ok=True)
    report_path = HISTORY / "blank_audit_report.json"
    temporary_report = report_path.with_suffix(".tmp")
    temporary_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(temporary_report, report_path)
    return report


def main() -> int:
    """Run the audit; repair mode is opt-in and remains targeted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="repair verified WDoc ink false-passes")
    parser.add_argument("--render-dpi", type=int, default=36)
    args = parser.parse_args()
    if args.render_dpi < 18 or args.render_dpi > 144:
        parser.error("--render-dpi must be between 18 and 144")
    report = audit_all(apply_repairs=args.repair, render_dpi=args.render_dpi)
    print(
        f"PDFs={report['pdf_count']} state_rows={report['state_rows']} "
        f"flagged={report['flagged_count']} repairs={len(report['repair_results'])}"
    )
    for result in report["repair_results"]:
        print(
            f"REPAIR_STATUS {result['uuid']} {result['status']} "
            f"pages={result.get('rendered_pages', '?')} strokes={result.get('stroke_count', '?')}"
        )
    print(f"Report: {HISTORY / 'blank_audit_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
