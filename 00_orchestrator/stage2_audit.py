"""Local-only reconciliation of exact export mappings and a read-only guest inventory."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "02_extracted_notes_pdf/ai_agend_history"
ORIGINAL = ROOT / "02_extracted_notes_pdf/notes/original"


def main() -> None:
    """Compare every live note, without treating size/count heuristics as proof."""
    import fitz
    inventory = json.loads((HISTORY / "stage2_wdoc_inventory.json").read_text())["notes"]
    guest = {row["uuid"]: row for row in inventory}
    state = sqlite3.connect((HISTORY / "incremental_state.sqlite").as_uri() + "?mode=ro", uri=True)
    state.execute("PRAGMA query_only=ON")
    columns = [row[1] for row in state.execute("PRAGMA table_info(exports)")]
    exports = {row["uuid"]: row for row in (dict(zip(columns, row)) for row in state.execute("SELECT * FROM exports"))}
    cached_empty = {row[0] for row in state.execute("SELECT uuid FROM empty_sources")}
    state.close()
    results = []
    for uuid, note in guest.items():
        row = exports.get(uuid)
        path = Path(row["path"]) if row else None
        data = {"uuid": uuid, "folder": note["folder"], "title": note["title"],
                "wdoc_payload_bytes": note["payload_bytes"], "raw_page_files": len(note["page_files"]),
                "empty_source_cached": uuid in cached_empty, "flags": [], "advisories": []}
        if not path or not path.is_file():
            data["flags"].append("no-tracked-pdf")
        else:
            data["path"] = path.relative_to(ORIGINAL).as_posix()
            data["pdf_bytes"] = path.stat().st_size
            try:
                with fitz.open(path) as pdf:
                    data["pdf_pages"] = len(pdf)
                    if row.get("pages") and row["pages"] != len(pdf):
                        data["flags"].append("export-state-page-count-mismatch")
                    if len(pdf) != len(note["page_files"]):
                        data["advisories"].append("raw-page-record-count-differs-not-proof")
                    # Uniform pages of any colour are featureless, not just pure white.
                    blank = []; text_chars = 0
                    for number, page in enumerate(pdf, 1):
                        text = page.get_text().strip(); text_chars += len(text)
                        if text:
                            continue
                        pix = page.get_pixmap(matrix=fitz.Matrix(.25, .25), colorspace=fitz.csGRAY, alpha=False)
                        samples = pix.samples
                        if samples and samples.count(samples[0]) == len(samples):
                            blank.append(number)
                    data["selectable_chars"] = text_chars
                    data["featureless_pages"] = blank
                    if len(blank) == len(pdf):
                        data["flags"].append("all-pages-featureless")
                if data["pdf_bytes"] < 10240:
                    data["advisories"].append("under-10KiB")
                if note["payload_bytes"] and data["pdf_bytes"] / note["payload_bytes"] < .01:
                    data["advisories"].append("pdf-under-1pct-wdoc-size-needs-source-probe")
            except Exception as error:
                data["flags"].append(f"unreadable:{type(error).__name__}")
        results.append(data)
    tracked = {str(Path(row["path"]).resolve()) for row in exports.values()}
    local = list(ORIGINAL.rglob("*.pdf"))
    report = {"live_notes": len(guest), "tracked_export_rows": len(exports),
              "local_original_pdfs": len(local), "unique_tracked_paths": len(tracked),
              "physical_pdfs_not_in_export_state": sum(str(p.resolve()) not in tracked for p in local),
              "tracked_live_notes": sum(uuid in exports for uuid in guest),
              "missing_tracked_live_notes": sum(uuid not in exports for uuid in guest),
              "flagged_notes": sum(bool(row["flags"]) for row in results),
              "advisory_notes": sum(bool(row["advisories"]) for row in results), "notes": results}
    output = HISTORY / "stage2_source_audit.json"
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "notes"}))


if __name__ == "__main__":
    main()
