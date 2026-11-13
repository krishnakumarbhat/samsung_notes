"""Summarize the corpus audit and the applied repairs into one report.

The summary joins the pre-repair audit, the repair log, and (optionally) a
post-repair audit so the remaining defects are explicit.  It is read-only: no
PDF, no database, and no report other than the output file is touched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
ORCHESTRATOR = PROJECT_ROOT / "00_orchestrator"
AUDIT = ORCHESTRATOR / "CORPUS_DEFECT_AUDIT.json"
POST_AUDIT = ORCHESTRATOR / "CORPUS_DEFECT_AUDIT_AFTER_REPAIR.json"
REPAIR_REPORT = ORCHESTRATOR / "STAGE3_REPAIR_REPORT.json"
SUMMARY = ORCHESTRATOR / "STAGE3_REPAIR_SUMMARY.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _defect_counts(audit: dict) -> dict:
    counts: Counter[str] = Counter()
    for item in audit.get("files", []):
        for finding in item.get("findings", []):
            counts[finding["code"]] += 1
    return dict(sorted(counts.items()))


def _workspace_bytes(root: Path = PROJECT_ROOT) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def summarize(audit_path: Path = AUDIT, post_path: Path = POST_AUDIT) -> dict:
    audit = _load(audit_path)
    post = _load(post_path)
    repair = _load(REPAIR_REPORT)
    results = repair.get("results", [])
    by_method: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    recovered_pages = 0
    text_gained = 0
    for result in results:
        by_status[result.get("status", "unknown")] += 1
        if result.get("status") != "ok":
            continue
        by_method[result.get("method", "unknown")] += 1
        verification = result.get("verification") or {}
        recovered_pages += int(verification.get("pages") or 0)
        text_gained += int(verification.get("text_chars") or 0)
    usage = shutil.disk_usage(PROJECT_ROOT)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "audit": {
            "path": str(audit_path),
            "files": audit.get("audit", {}),
            "defect_counts": _defect_counts(audit),
        },
        "post_repair_audit": {
            "path": str(post_path) if post else None,
            "files": post.get("audit", {}),
            "defect_counts": _defect_counts(post) if post else None,
        },
        "repairs": {
            "applied": repair.get("applied"),
            "status_counts": dict(sorted(by_status.items())),
            "method_counts": dict(sorted(by_method.items())),
            "pages_written": recovered_pages,
            "text_characters_in_repaired_output": text_gained,
            "originals_considered": repair.get("originals_considered"),
            "compressed_considered": repair.get("compressed_considered"),
            "state": repair.get("state"),
            "failures": [
                {
                    "local": item.get("local"),
                    "status": item.get("status"),
                    "reason": item.get("reason") or item.get("recovery_reason"),
                }
                for item in results
                if item.get("status") not in {"ok"}
            ],
        },
        "disk": {
            "workspace_bytes": _workspace_bytes(),
            "workspace_limit_bytes": 55_000_000_000,
            "volume_free_bytes": int(usage.free),
            "volume_total_bytes": int(usage.total),
        },
    }
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(payload, indent=2))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=AUDIT)
    parser.add_argument("--post-audit", type=Path, default=POST_AUDIT)
    args = parser.parse_args(argv)
    payload = summarize(args.audit, args.post_audit)
    print(json.dumps({
        "audit_defects": payload["audit"]["defect_counts"],
        "post_repair_defects": payload["post_repair_audit"]["defect_counts"],
        "repairs": payload["repairs"]["status_counts"],
        "methods": payload["repairs"]["method_counts"],
        "workspace_bytes": payload["disk"]["workspace_bytes"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
