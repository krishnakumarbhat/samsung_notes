#!/usr/bin/env python3
"""Download the pinned BAAI/bge-small-en-v1.5 model without Hugging Face Hub.

The workspace keeps the 133 MB weight file out of Git.  This script fetches the
exact revision recorded in ``SOURCE.json``, verifies the SHA-256 values from
``manifests/models.json`` when present, and writes atomically.

Usage
    python scripts/fetch_models.py
    python scripts/fetch_models.py --check
    python scripts/fetch_models.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "05_environments_and_venvs/models/bge-small-en-v1.5"
REPO = "BAAI/bge-small-en-v1.5"
DEFAULT_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
FILES = (
    "1_Pooling/config.json",
    "config.json",
    "config_sentence_transformers.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
)


def _sha256(path: Path) -> str:
    """Hash a downloaded file with bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected() -> dict[str, dict]:
    """Load the committed model manifest when it exists."""
    path = ROOT / "manifests/models.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["path"]: item for item in data.get("files", [])}


def _download(url: str, target: Path) -> None:
    """Stream one URL to a temporary file and publish it atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "sam-win-replication/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        with temporary.open("wb") as stream:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                written += len(block)
                if total:
                    percent = written * 100 // total
                    print(f"\r  {target.name}: {percent:3d}% ({written / 1e6:.1f}/{total / 1e6:.1f} MB)", end="")
    print()
    temporary.replace(target)


def main(argv: list[str] | None = None) -> int:
    """Check or download the model files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEST)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--check", action="store_true", help="verify local files without downloading")
    parser.add_argument("--force", action="store_true", help="replace existing files")
    parser.add_argument("--record", action="store_true", help="write manifests/models.json from the local files")
    args = parser.parse_args(argv)

    if args.record:
        records = []
        for name in FILES:
            target = args.dest / name
            if not target.is_file():
                print(f"cannot record missing file: {name}", file=sys.stderr)
                return 1
            records.append(
                {
                    "path": name,
                    "bytes": target.stat().st_size,
                    "sha256": _sha256(target),
                }
            )
        payload = {
            "schema": 1,
            "repo": REPO,
            "revision": args.revision,
            "source": f"https://huggingface.co/{REPO}/tree/{args.revision}",
            "files": records,
        }
        output = ROOT / "manifests/models.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {output} ({len(records)} files)")
        return 0

    expected = _expected()
    failures = 0
    for name in FILES:
        target = args.dest / name
        record = expected.get(name, {})
        if target.is_file() and not args.force:
            actual_size = target.stat().st_size
            wanted_size = record.get("bytes")
            if wanted_size and actual_size != wanted_size:
                print(f"FAIL {name}: size {actual_size} != {wanted_size}")
                failures += 1
            elif record.get("sha256") and _sha256(target) != record["sha256"]:
                print(f"FAIL {name}: sha256 mismatch")
                failures += 1
            else:
                print(f"OK   {name}")
            continue
        if args.check:
            print(f"MISS {name}")
            failures += 1
            continue

        url = f"https://huggingface.co/{REPO}/resolve/{args.revision}/{name}"
        print(f"GET  {name}")
        try:
            _download(url, target)
        except (urllib.error.URLError, OSError) as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if record.get("sha256") and _sha256(target) != record["sha256"]:
            print(f"FAIL {name}: downloaded file has the wrong sha256", file=sys.stderr)
            failures += 1

    if failures:
        print(f"{failures} model file(s) failed; check the network or manifests/models.json.", file=sys.stderr)
        return 1
    print(f"Model ready at {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())