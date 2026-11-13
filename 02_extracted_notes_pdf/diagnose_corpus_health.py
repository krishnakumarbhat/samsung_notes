"""Audit every exported Samsung Notes PDF for canvas and source defects.

The audit is deliberately evidence-first:

* every physical PDF under ``notes/original`` and ``notes/compressed`` is
  inspected, including regular files and symlink aliases;
* page 0, the middle page, and the last page are rendered with pypdfium2 for a
  fast pass; a file is deep-scanned on every page as soon as a black canvas or
  a blank-render candidate appears;
* source metadata is joined from ``wdoc/<UUID>`` through the read-only VM
  manifest and the local export state;
* symlinks are resolved and checked for broken targets, escapes outside the
  notes tree, and duplicate inode aliases.

The output is JSON, suitable for ``repair_defective_notes.py``.  No PDF and no
source database is modified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

import pypdfium2 as pdfium  # noqa: E402

from script import pdf_color_normalize as color  # noqa: E402

NOTES = ROOT / "notes"
ORIGINAL = NOTES / "original"
COMPRESSED = NOTES / "compressed"
HISTORY = ROOT / "ai_agend_history"
STATE_DB = HISTORY / "incremental_state.sqlite"
MANIFEST = HISTORY / "vm_manifest_probe.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "00_orchestrator/CORPUS_DEFECT_AUDIT.json"
UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
SUFFIX_RE = re.compile(
    r"__([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.pdf$",
    re.IGNORECASE,
)


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _state_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM exports")]
    finally:
        connection.close()


def _manifest_data(path: Path) -> dict:
    payload = _read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def _uuid_from_name(path: Path) -> str | None:
    match = UUID_RE.search(path.name)
    return match.group(1).lower() if match else None


def _build_source_indexes(state: list[dict], manifest: dict) -> tuple[dict[str, dict], dict[Path, dict]]:
    wdoc = {str(key).lower(): value for key, value in manifest.get("wdoc", {}).items()}
    notes = {str(row["UUID"]).lower(): row for row in manifest.get("note_rows", [])}
    for uuid, row in notes.items():
        row["_files"] = wdoc.get(uuid, [])
    by_path: dict[Path, dict] = {}
    for row in state:
        raw = row.get("path")
        if not raw:
            continue
        uuid = str(row.get("uuid", "")).lower()
        record = {
            "uuid": uuid,
            "note": notes.get(uuid),
            "files": wdoc.get(uuid, []),
            "source": "state",
            "state": row,
        }
        try:
            by_path[Path(raw).resolve()] = record
        except OSError:
            continue
    return notes, by_path


def _source_for(path: Path, uuid: str | None, notes: dict[str, dict], by_path: dict[Path, dict]) -> dict:
    record = by_path.get(path.resolve()) if path.exists() else None
    if record is None and uuid:
        note = notes.get(uuid)
        if note:
            record = {"uuid": uuid, "note": note, "files": note.get("_files", []),
                      "source": "uuid", "state": None}
    if record is None:
        return {"uuid": uuid, "note": None, "files": [], "source": "unmapped", "state": None}
    return record


def _source_facts(record: dict) -> dict:
    note = record.get("note") or {}
    files = record.get("files") or []
    page_files = [item for item in files if str(item.get("rel", "")).lower().endswith(".page")]
    media_pdfs = [item for item in files if str(item.get("rel", "")).lower().endswith(".pdf")]
    page_color = note.get("PageColor")
    theme = note.get("ThemeMode")
    return {
        "uuid": record.get("uuid"),
        "title": note.get("Title"),
        "folder": note.get("Folder"),
        "deleted_status": note.get("DeletedStatus"),
        "page_color": _hex_argb(page_color) if page_color is not None else None,
        "page_color_inverted": _hex_argb(note.get("PageColorInverted"))
        if note.get("PageColorInverted") is not None else None,
        "theme_mode": theme,
        "source_pages": len(page_files),
        "source_page_bytes": sum(int(item.get("size", 0)) for item in page_files),
        "source_media_pdfs": len(media_pdfs),
        "source_media_bytes": sum(int(item.get("size", 0)) for item in media_pdfs),
        "source_payload_bytes": sum(int(item.get("size", 0)) for item in files),
        "page_files": page_files,
        "mapping": record.get("source"),
    }


def _hex_argb(value: object) -> str:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return "#%02x%02x%02x" % ((number >> 16) & 255, (number >> 8) & 255, number & 255)


def _render_page(page, scale: float, index: int) -> dict:
    bitmap = page.render(scale=scale, grayscale=True)
    try:
        array = bitmap.to_numpy().reshape(bitmap.height, bitmap.width)
        histogram = __import__("numpy").bincount(array.ravel(), minlength=256)
        try:
            text_page = page.get_textpage()
            text = text_page.get_text_bounded()
            text_page.close()
        except Exception:
            text = ""
        return {
            "index": index,
            "width": bitmap.width,
            "height": bitmap.height,
            "mean": round(float(array.mean()), 4),
            "std": round(float(array.std()), 4),
            "min": int(array.min()),
            "max": int(array.max()),
            "dark_fraction": round(float(histogram[:16].sum() / array.size), 5),
            "white_fraction": round(float(histogram[240:].sum() / array.size), 5),
            "text_chars": len(text),
        }
    finally:
        bitmap.close()


def _sample_indices(count: int) -> list[int]:
    if count <= 0:
        return []
    return sorted({0, count // 2, count - 1})


def scan_pdf(path: Path, *, scale: float = 24 / 72, deep: bool = True) -> dict:
    """Render a PDF's sample pages and deep-scan it when a defect is visible."""
    result: dict = {"page_count": 0, "pages": [], "deep_scanned": False,
                    "error": None, "metadata": {}}
    document = None
    try:
        document = pdfium.PdfDocument(str(path))
        result["page_count"] = len(document)
        try:
            result["metadata"] = {str(k): str(v) for k, v in document.get_metadata_dict().items()}
        except Exception:
            pass
        indices = _sample_indices(len(document))
        for index in indices:
            page = document[index]
            try:
                result["pages"].append(_render_page(page, scale, index))
            finally:
                page.close()
        suspicious = any(
            item["mean"] < 30.0
            or (item["text_chars"] == 0 and item["std"] < 1.0 and item["mean"] > 200.0)
            for item in result["pages"]
        )
        if deep and suspicious and len(document) > len(indices):
            result["pages"] = []
            for index in range(len(document)):
                page = document[index]
                try:
                    result["pages"].append(_render_page(page, scale, index))
                finally:
                    page.close()
            result["deep_scanned"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if document is not None:
            document.close()
    return result


def _blank_candidate(item: dict) -> bool:
    return item["text_chars"] == 0 and item["std"] < 1.0 and item["mean"] > 200.0


def classify(path: Path, root: str, scan: dict, source: dict, symlink: dict | None) -> list[dict]:
    findings: list[dict] = []
    pages = scan.get("pages", [])
    black_pages = [item["index"] for item in pages if item["mean"] < 30.0]
    blank_pages = [item["index"] for item in pages if _blank_candidate(item)]
    source_pages = int(source.get("source_pages") or 0)
    media_pdfs = int(source.get("source_media_pdfs") or 0)
    text_total = sum(int(item.get("text_chars", 0)) for item in pages)
    if scan.get("error"):
        findings.append({"code": "DEFECT_UNREADABLE_PDF", "severity": "high",
                         "detail": scan["error"]})
    if black_pages:
        findings.append({
            "code": "DEFECT_BLACK_CANVAS",
            "severity": "high",
            "pages": black_pages,
            "black_fraction": round(len(black_pages) / max(1, len(pages)), 4),
            "detail": "rendered page luminance is below 30/255",
        })
    if blank_pages and source_pages and blank_pages[0] < source_pages:
        findings.append({
            "code": "DEFECT_BLANK_RENDER_CANDIDATE",
            "severity": "medium",
            "pages": blank_pages,
            "detail": "uniform white page overlaps a source page record; verify source strokes before repair",
        })
    if media_pdfs and black_pages:
        distinct = sum(1 for item in pages if item["mean"] >= 30.0)
        if not distinct and (text_total == 0 or len(black_pages) >= max(1, len(pages) - 1)):
            findings.append({
                "code": "DEFECT_MISSING_BASE_PDF",
                "severity": "high",
                "pages": black_pages,
                "detail": "source media PDF exists but the export has no visible base paper",
            })
    if symlink and symlink.get("broken"):
        findings.append({"code": "DEFECT_SYMLINK", "severity": "high",
                         "detail": "broken symlink"})
    if symlink and symlink.get("outside"):
        findings.append({"code": "DEFECT_SYMLINK", "severity": "high",
                         "detail": "symlink target escapes the notes tree"})
    if symlink and symlink.get("duplicate_inode"):
        findings.append({"code": "DEFECT_SYMLINK", "severity": "low",
                         "detail": "symlink shares an inode with another PDF alias"})
    return findings


def _symlink_facts(path: Path) -> dict:
    facts = {"is_symlink": path.is_symlink(), "target": None, "broken": False,
             "outside": False, "duplicate_inode": False, "inode": None}
    if not path.is_symlink():
        return facts
    try:
        target = path.resolve(strict=True)
        facts["target"] = str(target)
        facts["inode"] = os.stat(target).st_ino
        facts["outside"] = not target.is_relative_to(NOTES.resolve())
    except OSError:
        facts["broken"] = True
    return facts


def _walk_pdfs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.pdf") if p.is_file() or p.is_symlink())


def _verify_blank_candidates(files: list[dict], *, cache: Path | None = None) -> dict:
    """Check blank-render candidates against the source stroke counts.

    A locally blank page is only a defect when the matching source page
    actually carries strokes.  The per-page inventory comes from the CLI's own
    probe of the read-only WDoc directory, so a genuinely empty page is
    reported as *not* a defect.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "script"))
    try:
        import page_delta as pd
        import pdf_template_recovery as rec
    except Exception as exc:  # pragma: no cover - environment without the CLI
        return {"status": "unavailable", "reason": str(exc)}
    from script import incremental_export as ie

    cache = cache or Path("/tmp/opencode/wdoc_repair")
    cache.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for item in files:
        candidates = [finding for finding in item.get("findings", [])
                      if finding["code"] == "DEFECT_BLANK_RENDER_CANDIDATE"]
        if not candidates:
            continue
        uuid = str((item.get("source") or {}).get("uuid") or "").lower()
        if not uuid:
            results[item["path"]] = {"status": "unverified", "reason": "no source UUID"}
            continue
        source = cache / uuid
        if not (source / "note.note").is_file():
            remote = ("notes@127.0.0.1:C:/Users/notes/AppData/Local/Packages/"
                      "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState/"
                      f"wdoc/{uuid}")
            try:
                ie.execute(ie.transport("scp") + ["-r", remote, str(cache)], 900)
            except Exception as exc:
                results[item["path"]] = {"status": "unverified", "reason": str(exc)[:200]}
                continue
        work = cache.parent / "blank_probe" / uuid
        work.mkdir(parents=True, exist_ok=True)
        try:
            archive = pd._build_archive(source, work)
            _count, pages, _warnings = rec._probe(archive, work)
        except Exception as exc:
            results[item["path"]] = {"status": "unverified", "reason": str(exc)[:200]}
            continue
        strokes = {page["index"]: page["strokes"] for page in pages}
        confirmed = [index for index in candidates[0].get("pages", [])
                     if strokes.get(index, 0) > 0]
        results[item["path"]] = {
            "status": "confirmed" if confirmed else "not_a_defect",
            "source_pages": len(pages),
            "source_strokes": strokes,
            "confirmed_pages": confirmed,
        }
        if confirmed:
            candidates[0]["code"] = "DEFECT_BLANK_RENDER"
            candidates[0]["pages"] = confirmed
            candidates[0]["detail"] = (
                "source page carries strokes while the exported page renders blank"
            )
        else:
            candidates[0]["code"] = "BLANK_PAGE_VERIFIED_EMPTY"
            candidates[0]["detail"] = "source page has no strokes; the blank page is faithful"
    return results


def audit(*, output: Path, deep: bool = True, limit: int | None = None,
          verify_blank: bool = False) -> dict:
    """Run the full audit and write the JSON report."""
    manifest = _manifest_data(MANIFEST)
    state = _state_rows(STATE_DB)
    notes, by_path = _build_source_indexes(state, manifest)
    paths = _walk_pdfs(ORIGINAL) + _walk_pdfs(COMPRESSED)
    if limit:
        paths = paths[:limit]
    inode_counts: Counter[tuple[int, int]] = Counter()
    symlink_facts: dict[Path, dict] = {}
    for path in paths:
        facts = _symlink_facts(path)
        symlink_facts[path] = facts
        if facts["inode"] is not None and facts["is_symlink"]:
            inode_counts[(os.stat(path.resolve()).st_dev, facts["inode"])] += 1
    for facts in symlink_facts.values():
        if facts["inode"] is not None and facts["is_symlink"]:
            stats = os.stat(Path(facts["target"]))
            facts["duplicate_inode"] = inode_counts[(stats.st_dev, facts["inode"])] > 1

    files: list[dict] = []
    summary = Counter()
    page_total = 0
    deep_total = 0
    for index, path in enumerate(paths, 1):
        root = "original" if path.is_relative_to(ORIGINAL) else "compressed"
        symlink = symlink_facts[path]
        uuid = _uuid_from_name(path)
        record = _source_for(path, uuid, notes, by_path)
        source = _source_facts(record)
        if symlink["is_symlink"] and path.exists():
            target_uuid = _uuid_from_name(Path(symlink["target"])) if symlink["target"] else None
            uuid = uuid or target_uuid
        scan = scan_pdf(path, deep=deep)
        page_total += len(scan.get("pages", []))
        deep_total += 1 if scan.get("deep_scanned") else 0
        findings = classify(path, root, scan, source, symlink)
        for finding in findings:
            summary[finding["code"]] += 1
        files.append({
            "path": str(path),
            "relative": str(path.relative_to(NOTES)),
            "root": root,
            "size": path.stat().st_size if path.exists() else 0,
            "sha256": _sha256(path) if path.exists() and not symlink["is_symlink"] else None,
            "symlink": symlink,
            "source": {k: v for k, v in source.items() if k not in {"page_files"}},
            "page_count": scan.get("page_count", 0),
            "pages": scan.get("pages", []),
            "deep_scanned": scan.get("deep_scanned", False),
            "findings": findings,
        })
        if index % 25 == 0:
            print(f"[{index}/{len(paths)}] {path}", flush=True)

    black_files = sum(1 for item in files if any(
        finding["code"] == "DEFECT_BLACK_CANVAS" for finding in item["findings"]))
    black_pages = sum(len(finding.get("pages", [])) for item in files
                      for finding in item["findings"]
                      if finding["code"] == "DEFECT_BLACK_CANVAS")
    blank_verification: dict[str, dict] = {}
    if verify_blank:
        blank_verification = _verify_blank_candidates(files)
        summary = Counter()
        for item in files:
            for finding in item["findings"]:
                summary[finding["code"]] += 1
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes_root": str(NOTES),
        "audit": {
            "files": len(files),
            "originals": sum(1 for item in files if item["root"] == "original"),
            "compressed": sum(1 for item in files if item["root"] == "compressed"),
            "pages_inspected": page_total,
            "deep_scanned_files": deep_total,
            "black_files": black_files,
            "black_pages": black_pages,
            "symlinks": sum(1 for item in files if item["symlink"]["is_symlink"]),
            "symlink_defects": sum(1 for item in files
                                   if any(f["code"] == "DEFECT_SYMLINK" for f in item["findings"])),
            "defect_counts": dict(sorted(summary.items())),
            "blank_verification": {
                key: value.get("status") for key, value in blank_verification.items()
            },
        },
        "files": files,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=False))
    temporary.replace(output)
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-deep", action="store_true", help="sample only, for a quick inventory")
    parser.add_argument("--verify-blank", action="store_true",
                        help="check blank-render candidates against the source stroke counts")
    parser.add_argument("--limit", type=int, help="limit the number of physical PDFs (debugging)")
    args = parser.parse_args(argv)
    payload = audit(output=args.output, deep=not args.no_deep, limit=args.limit,
                    verify_blank=args.verify_blank)
    print(json.dumps(payload["audit"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
