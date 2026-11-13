"""Repair audited Samsung Notes PDF defects with verified, reversible writes.

Two repair paths are used, in this order:

1. **Embedded-PDF recovery.**  When ``wdoc/<UUID>/media`` contains the source
   base PDF, ``pdf_template_recovery.recover`` rebuilds the visible page order
   from the pristine document.  Ink over embedded paper is composed as a
   transparent vector layer; the CLI page background is stripped before the
   overlay.  This is the only path that can restore missing base paper.
2. **Light-canvas normalization.**  For pure handwriting, legacy image pages,
   and notes whose source binding cannot be proved, dark backgrounds are
   replaced with white and ink colours are reversed with the same HSL rule as
   the renderer's light theme.  Light pages are not touched.

Every candidate is written to a temporary directory, rendered with pypdfium2,
and checked for page count, text retention, and residual black pages before it
is published.  The original file is first copied to a rollback directory; a
failed verification restores it or leaves the original untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from python_runtime_bootstrap import bootstrap_python_paths  # noqa: E402

bootstrap_python_paths(Path(__file__).resolve())

from script import incremental_export as ie  # noqa: E402
from script import pdf_color_normalize as color  # noqa: E402
from script import pdf_template_recovery as recovery  # noqa: E402

NOTES = ROOT / "notes"
ORIGINAL = NOTES / "original"
COMPRESSED = NOTES / "compressed"
AUDIT = PROJECT_ROOT / "00_orchestrator/CORPUS_DEFECT_AUDIT.json"
REPORT = PROJECT_ROOT / "00_orchestrator/STAGE3_REPAIR_REPORT.json"
LOG = PROJECT_ROOT / "00_orchestrator/REPAIR_LOG.jsonl"
WORK = Path("/tmp/opencode/stage3_repair")
ROLLBACK = Path("/tmp/opencode/stage3_rollback")
WDOC_CACHE = Path("/tmp/opencode/wdoc_repair")
RECOVERED = Path("/tmp/opencode/stage3_recovered")
MAX_WORKSPACE_BYTES = 55_000_000_000
MIN_FREE_BYTES = 15 * 1024 ** 3
GUEST_ROOT = (
    "C:/Users/notes/AppData/Local/Packages/"
    "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy/LocalState/wdoc"
)


def _read_audit(path: Path) -> dict:
    return json.loads(path.read_text())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_bytes(root: Path = PROJECT_ROOT) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def _free_bytes(root: Path = PROJECT_ROOT) -> int:
    stats = shutil.disk_usage(root)
    return int(stats.free)


def _can_write() -> bool:
    return _workspace_bytes() < MAX_WORKSPACE_BYTES and _free_bytes() >= MIN_FREE_BYTES


def _fetch_wdoc(uuid: str, cache: Path = WDOC_CACHE) -> Path:
    """Copy one WDoc directory from the running VM; return its local path."""
    destination = cache / uuid
    marker = destination / "note.note"
    if marker.is_file():
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    remote = f"notes@127.0.0.1:{GUEST_ROOT}/{uuid}"
    ie.execute(ie.transport("scp") + ["-r", remote, str(cache)], timeout=900)
    if not marker.is_file():
        raise RuntimeError(f"WDoc copy for {uuid} did not contain note.note")
    return destination


def _record(log: list[dict], **values: object) -> None:
    entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), **values}
    log.append(entry)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def _verify(path: Path, *, expected_pages: int, before_text: int = 0,
            before_low: Iterable[int] | None = None,
            before_black: Iterable[int] | None = None) -> dict:
    """Render every page and reject residual black canvases or text loss."""
    stats = color.render_page_stats(path)
    black = [item["index"] for item in stats if item["mean"] < 30.0]
    text = sum(int(item["text_chars"]) for item in stats)
    low = [item["index"] for item in stats if item["mean"] < 120.0]
    problems: list[str] = []
    if len(stats) != expected_pages:
        problems.append(f"page count {len(stats)} != {expected_pages}")
    if black:
        problems.append(f"black pages remain: {black[:20]}")
    if before_text and text + 3 < int(before_text * 0.98):
        problems.append(f"text chars fell from {before_text} to {text}")
    allowed = set(before_low or []) | set(before_black or [])
    new_low = sorted(set(low) - allowed)
    if new_low:
        problems.append(f"new low-luminance pages: {new_low[:20]}")
    return {
        "pages": len(stats),
        "text_chars": text,
        "black_pages": black,
        "low_luminance_pages": low,
        "new_low_luminance_pages": new_low,
        "mean_min": min((item["mean"] for item in stats), default=0.0),
        "mean_max": max((item["mean"] for item in stats), default=0.0),
        "problems": problems,
    }


def _source_page_mean(source: Path, page_index: int) -> float | None:
    """Render one page of an embedded source PDF; used to prove a dark page."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(source))
    try:
        if page_index < 0 or page_index >= len(document):
            return None
        page = document[page_index]
        try:
            bitmap = page.render(scale=24 / 72, grayscale=True)
            array = bitmap.to_numpy()
            return float(array.mean())
        finally:
            page.close()
    finally:
        document.close()


def _verify_recovered(
    path: Path,
    *,
    expected_pages: int,
    before_text: int,
    manifest_path: Path,
) -> dict:
    """Verify a recovered note: black pages may only be faithful source pages."""
    stats = color.render_page_stats(path)
    black = [item["index"] for item in stats if item["mean"] < 30.0]
    text = sum(int(item["text_chars"]) for item in stats)
    problems: list[str] = []
    manifest = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
    binding = {int(key): value for key, value in (manifest.get("binding") or {}).items()}
    sources = manifest.get("sources") or {}
    unexpected: list[int] = []
    for index in black:
        entry = binding.get(index)
        if not entry:
            unexpected.append(index)
            continue
        name, number = entry
        source = Path(sources.get(name, ""))
        mean = _source_page_mean(source, int(number)) if source.is_file() else None
        if mean is None or mean >= 30.0:
            unexpected.append(index)
    if len(stats) != expected_pages:
        problems.append(f"page count {len(stats)} != {expected_pages}")
    if unexpected:
        problems.append(f"unexpected black pages: {unexpected[:20]}")
    if before_text and text + 3 < int(before_text * 0.98):
        problems.append(f"text chars fell from {before_text} to {text}")
    return {
        "pages": len(stats),
        "text_chars": text,
        "black_pages": black,
        "faithful_black_pages": [index for index in black if index not in unexpected],
        "unexpected_black_pages": unexpected,
        "mean_min": min((item["mean"] for item in stats), default=0.0),
        "mean_max": max((item["mean"] for item in stats), default=0.0),
        "problems": problems,
    }


def _recover_candidate(local: Path, uuid: str, work: Path) -> dict:
    """Try embedded-PDF recovery; return a result dict without writing local."""
    try:
        source = _fetch_wdoc(uuid)
    except Exception as exc:
        return {"status": "unsupported", "reason": f"WDoc fetch failed: {type(exc).__name__}: {exc}"}
    attempt = work / "recover"
    if attempt.exists():
        shutil.rmtree(attempt)
    attempt.mkdir(parents=True)
    try:
        recovered, count, warnings = recovery.recover(
            source, attempt, allow_ink_overlay=True,
        )
    except recovery.UnsupportedRecovery as exc:
        return {"status": "unsupported", "reason": str(exc)}
    except Exception as exc:  # keep the batch alive; report the exact failure
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    before_text = sum(int(item.get("text_chars", 0)) for item in color.render_page_stats(local))
    verified = _verify_recovered(
        recovered, expected_pages=count, before_text=before_text,
        manifest_path=attempt / "recovery-binding.json",
    )
    if verified["problems"]:
        return {"status": "verification_failed", "reason": "; ".join(verified["problems"]),
                "verification": verified, "recovered": str(recovered)}
    output = recovered
    remaining = verified.get("black_pages") or []
    if remaining:
        # The embedded document itself carries a dark canvas (for example a
        # dark-theme note page kept as an image).  Repaint that canvas on white
        # without touching the pristine vector pages around it.
        light = work / "recovered-light.pdf"
        try:
            color.normalize_pdf(recovered, light, page_indices=remaining, dark_pages=remaining)
        except Exception as exc:
            return {"status": "error", "reason": f"source-page normalization failed: {exc}"}
        second = _verify_recovered(
            light, expected_pages=count, before_text=before_text,
            manifest_path=attempt / "recovery-binding.json",
        )
        if second["problems"]:
            return {"status": "verification_failed", "reason": "; ".join(second["problems"]),
                    "verification": second, "recovered": str(recovered)}
        verified = second
        output = light
    return {
        "status": "ok",
        "path": str(output),
        "pages": count,
        "recovered_from": str(recovered),
        "warnings": warnings,
        "verification": verified,
        "method": "embedded-pdf-recovery",
    }


def _normalize_candidate(local: Path, work: Path, page_indices: Iterable[int] | None = None,
                         dark_pages: Iterable[int] | None = None) -> dict:
    """Normalize one local PDF without source recovery."""
    before_stats = color.render_page_stats(local)
    before_text = sum(int(item["text_chars"]) for item in before_stats)
    before_black = [item["index"] for item in before_stats if item["mean"] < 30.0]
    before_low = [item["index"] for item in before_stats if item["mean"] < 120.0]
    output = work / "normalized.pdf"
    try:
        report = color.normalize_pdf(local, output, page_indices=page_indices,
                                      dark_pages=dark_pages)
    except Exception as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    verified = _verify(output, expected_pages=len(before_stats), before_text=before_text,
                       before_low=before_low, before_black=before_black)
    if verified["problems"]:
        return {"status": "verification_failed", "reason": "; ".join(verified["problems"]),
                "verification": verified}
    return {
        "status": "ok",
        "path": str(output),
        "pages": len(before_stats),
        "before_black_pages": before_black,
        "repaired_pages": report.repaired_pages,
        "verification": verified,
        "method": "light-canvas-normalization",
    }


def _update_state(results: list[dict]) -> dict:
    """Refresh the local export state so the repairs are not re-exported.

    Only the local bookkeeping database is touched; the production
    ``Storage.sqlite`` on the VM stays read-only.
    """
    state_db = ROOT / "ai_agend_history/incremental_state.sqlite"
    if not state_db.is_file():
        return {"updated": 0}
    connection = sqlite3.connect(state_db.resolve().as_uri())
    connection.row_factory = sqlite3.Row
    updated = 0
    try:
        for result in results:
            published = result.get("published")
            if not published:
                continue
            path = published["path"]
            if result.get("compressed"):
                cursor = connection.execute(
                    "UPDATE exports SET derived_sha256=? WHERE derived_path=?",
                    (published["sha256"], path),
                )
            else:
                cursor = connection.execute(
                    "UPDATE exports SET sha256=?, pages=?, renderer=? WHERE path=?",
                    (published["sha256"], int(result.get("pages") or 0),
                     "stage3-light-canvas", path),
                )
            updated += cursor.rowcount
        connection.commit()
    finally:
        connection.close()
    return {"updated": updated}


def _cleanup_work(work: Path, keep: bool = False) -> None:
    """Drop bulky intermediates; keep small failure artefacts for inspection."""
    if not work.exists():
        return
    if keep:
        size = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
        if size < 50_000_000:
            return
    shutil.rmtree(work, ignore_errors=True)


def _publish(local: Path, candidate: Path, uuid: str) -> dict:
    """Back up and atomically replace a local PDF, preserving permissions."""
    rollback = ROLLBACK / uuid / local.relative_to(NOTES)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    if not rollback.exists():
        shutil.copy2(local, rollback)
    temporary = local.with_name(f".{local.name}.repair-tmp")
    shutil.copy2(candidate, temporary)
    os.replace(temporary, local)
    return {"path": str(local), "rollback": str(rollback),
            "sha256": _sha256(local), "bytes": local.stat().st_size}


def _compressed_aliases(audit: dict, uuid: str) -> list[Path]:
    """Return regular compressed PDFs associated with a source UUID."""
    paths: list[Path] = []
    for item in audit.get("files", []):
        if item.get("root") != "compressed" or item.get("symlink", {}).get("is_symlink"):
            continue
        source = item.get("source") or {}
        if str(source.get("uuid") or "").lower() == uuid:
            paths.append(Path(item["path"]))
    return paths


def repair(*, apply: bool = False, uuid_filter: str | None = None,
           limit: int | None = None) -> dict:
    audit = _read_audit(AUDIT)
    originals = [
        item for item in audit.get("files", [])
        if item.get("root") == "original"
        and any(f["code"] == "DEFECT_BLACK_CANVAS" for f in item.get("findings", []))
    ]
    if uuid_filter:
        originals = [item for item in originals
                     if str((item.get("source") or {}).get("uuid") or "").lower() == uuid_filter.lower()]
    if limit:
        originals = originals[:limit]
    log: list[dict] = []
    results: list[dict] = []
    repaired_originals: dict[str, Path] = {}
    recovered_outputs: dict[str, Path] = {}
    for index, item in enumerate(originals, 1):
        if not _can_write():
            _record(log, uuid=(item.get("source") or {}).get("uuid"), status="stopped",
                    reason="workspace or free-space cap")
            break
        local = Path(item["path"])
        uuid = str((item.get("source") or {}).get("uuid") or "").lower()
        work = WORK / (uuid or f"unmapped-{index:03d}")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        source = item.get("source") or {}
        media = int(source.get("source_media_pdfs") or 0)
        result = None
        recovery_reason = None
        if uuid and media:
            result = _recover_candidate(local, uuid, work / "recovery")
            if result["status"] != "ok":
                recovery_reason = result.get("reason")
                _record(log, uuid=uuid, path=str(local), status="recovery_not_used",
                        reason=recovery_reason)
        if result is None or result.get("status") != "ok":
            page_indices = [p["index"] for p in item.get("pages", []) if p["mean"] < 30.0]
            result = _normalize_candidate(local, work / "normalize", page_indices or None,
                                          page_indices or None)
            result["fallback_after_recovery"] = bool(media)
            if recovery_reason:
                result["recovery_reason"] = recovery_reason
        result.update({"uuid": uuid, "local": str(local), "title": source.get("title"),
                       "media_pdfs": media, "source_pages": source.get("source_pages")})
        if result.get("status") == "ok":
            if apply:
                try:
                    result["published"] = _publish(local, Path(result["path"]), uuid or f"unmapped-{index:03d}")
                    repaired_originals[uuid] = local
                    if result.get("method") == "embedded-pdf-recovery" and uuid:
                        keep = RECOVERED / f"{uuid}.pdf"
                        keep.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(result["path"], keep)
                        recovered_outputs[uuid] = keep
                except Exception as exc:
                    result["status"] = "publish_failed"
                    result["reason"] = f"{type(exc).__name__}: {exc}"
            else:
                result["dry_run"] = True
        _record(log, **{k: v for k, v in result.items() if k not in {"verification"}})
        results.append(result)
        _cleanup_work(work, keep=result.get("status") not in {"ok"})
        print(f"[{index}/{len(originals)}] {result['status']} {local.name} "
              f"{result.get('method', '')}", flush=True)
    # Compressed derivatives: preserve OCR, normalize only their own black pages.
    compressed = [
        item for item in audit.get("files", [])
        if item.get("root") == "compressed" and not item.get("symlink", {}).get("is_symlink")
        and any(f["code"] == "DEFECT_BLACK_CANVAS" for f in item.get("findings", []))
    ]
    if uuid_filter:
        compressed = [item for item in compressed
                      if str((item.get("source") or {}).get("uuid") or "").lower() == uuid_filter.lower()]
    for index, item in enumerate(compressed, 1):
        if not _can_write():
            _record(log, path=item["path"], status="stopped", reason="workspace or free-space cap")
            break
        local = Path(item["path"])
        uuid = str((item.get("source") or {}).get("uuid") or "").lower()
        work = WORK / f"compressed-{uuid or 'unmapped'}-{index:03d}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        page_indices = [p["index"] for p in item.get("pages", []) if p["mean"] < 30.0]
        reused = recovered_outputs.get(uuid)
        if reused is not None and reused.is_file():
            before_stats = color.render_page_stats(local)
            verified = _verify(reused, expected_pages=len(before_stats))
            if not verified["problems"]:
                result = {
                    "status": "ok",
                    "path": str(reused),
                    "pages": len(before_stats),
                    "verification": verified,
                    "method": "embedded-pdf-recovery (shared with the original)",
                }
            else:
                result = _normalize_candidate(local, work, page_indices or None,
                                              page_indices or None)
        else:
            result = _normalize_candidate(local, work, page_indices or None,
                                          page_indices or None)
        result.update({"uuid": uuid, "local": str(local), "compressed": True,
                       "title": (item.get("source") or {}).get("title")})
        if result.get("status") == "ok" and apply:
            try:
                result["published"] = _publish(local, Path(result["path"]), uuid or f"compressed-{index:03d}")
            except Exception as exc:
                result["status"] = "publish_failed"
                result["reason"] = f"{type(exc).__name__}: {exc}"
        _record(log, **{k: v for k, v in result.items() if k not in {"verification"}})
        results.append(result)
        _cleanup_work(work, keep=result.get("status") not in {"ok"})
        print(f"[compressed {index}/{len(compressed)}] {result['status']} {local.name}",
              flush=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "applied": apply,
        "originals_considered": len(originals),
        "compressed_considered": len(compressed),
        "results": results,
        "state": _update_state(results) if apply else {"updated": 0},
        "workspace_bytes": _workspace_bytes(),
        "free_bytes": _free_bytes(),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="publish verified repairs")
    parser.add_argument("--uuid", help="only one source UUID")
    parser.add_argument("--limit", type=int, help="limit original candidates")
    parser.add_argument("--dry-run", action="store_true", help="explicitly do not publish")
    args = parser.parse_args(argv)
    apply = bool(args.apply) and not args.dry_run
    payload = repair(apply=apply, uuid_filter=args.uuid, limit=args.limit)
    print(json.dumps({
        "applied": payload["applied"],
        "originals": payload["originals_considered"],
        "compressed": payload["compressed_considered"],
        "workspace_bytes": payload["workspace_bytes"],
        "free_bytes": payload["free_bytes"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
