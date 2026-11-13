# Manifests

Small, reviewable metadata that describes the local-only payload. None of these
files contains note text, credentials or model weights.

| File | Generator | Purpose |
|---|---|---|
| `local_assets.json` | `scripts/assets_manifest.py generate --public-summary --no-hash` | Aggregate-only inventory; no local filenames or paths |
| `corpus.json` | `scripts/corpus_manifest.py --no-hash` | Aggregate PDF/page/text counts, without per-note names |
| `models.json` | `scripts/fetch_models.py --record` | Pinned Hugging Face revision and file checksums |
| `environment.json` | `scripts/environment_report.py` | Platform, interpreter, git commit and tested package versions |

## Commands

```bash
source 00_orchestrator/runtime.sh

# Regenerate after adding notes or changing the model
python scripts/assets_manifest.py generate --hash
python scripts/assets_manifest.py generate --public-summary --no-hash
python scripts/corpus_manifest.py --no-hash
python scripts/fetch_models.py --record
python scripts/environment_report.py

# Verify a restored machine
python scripts/assets_manifest.py verify --output manifests/local_assets.private.json --hash
python scripts/fetch_models.py --check
```

## Policy

- `local_assets.json` contains aggregate counts only. The detailed
  `local_assets.private.json` contains local paths and belongs in encrypted
  personal backups, never in Git.
- SHA-256 is computed with bounded memory and is optional for the 34 GB VM disk
  (`hash_by_default: false`); enable it with `--hash` when you need an exact
  transfer check.
- A manifest is a snapshot, not a backup. Restore the payload first, then verify.
- `corpus_manifest.py --include-paths` is explicitly private because PDF
  filenames and hashes can reveal information about personal notes.
- If a path is intentionally removed, regenerate the manifest and commit the
  change; do not hand-edit checksums.
- `pytest tests/test_manifests.py` checks the schema, totals and model pin without
  requiring the payload.
