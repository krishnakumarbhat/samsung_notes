# Data and assets

Git is used for **code and evidence**, not for the note corpus. This page records
what exists locally, why it is excluded, and how to move it safely.

## Inventory (reference machine, 2026-11-13)

| Group | Path | Files | Size | In Git? | Restore |
|---|---|---:|---:|---|---|
| Windows VM | `01_vm_windows_sync/vm/` | 4 | 34.79 GB | No | Copy disk or rebuild |
| Exported notes | `02_extracted_notes_pdf/notes/` | 539 | 12.84 GB | No | Bundle / rsync |
| Export history | `02_extracted_notes_pdf/ai_agend_history/` | 352 | 379 MB | Partly | Bundle / rsync |
| BGE-small model | `05_environments_and_venvs/models/` | 11 | 134 MB | Config only | `fetch_models.py` |
| App data | `04_learning_recall_app/my_notes_graph/data/` | 22 | 171 MB | No (indexes/DBs) | Bundle or rebuild |
| Notes mirror | `04_learning_recall_app/notes_mirror/` | 1 | 202 KB | No | Bundle |
| OCR visual cache | `03_rtx_compression_ocr/ocr_visual_cache/` | 106 | 401 KB | No | Regenerable |

The reference inventory is a snapshot, not a promise that payloads are present
in a fresh clone. The public manifests deliberately omit per-note filenames,
paths, hashes, titles and text. A detailed inventory is generated locally into
an ignored `*.private.json` file and can travel only inside the encrypted
personal transfer bundle.

## Why the payload is not in Git

1. **GitHub's hard limit is 100 MB per file.** The VM disk and 34 PDFs exceed it.
2. **A 48 GB repository is not a backup.** Clone, fetch and CI become unusable,
   and GitHub warns above 1 GB / blocks above 5 GB.
3. **Personal data.** The PDFs contain notes, books, interview material and
   health topics. The repository is public; publishing them would be
   irreversible.
4. **Model weights are reproducible** from a pinned public revision.
5. **Python environments are machine-specific** and contain CUDA libraries that
   must match the driver.

`scripts/verify_repo.py` enforces a **99,000,000-byte** tracked-file ceiling and
fails the push if an `.env` or credential pattern is tracked.

## Manifest schema

`manifests/local_assets.json` is an aggregate-only public inventory. The
complete local inventory is written to the ignored
`manifests/local_assets.private.json`:

```json
{
  "schema": 1,
  "generated_at": "2026-11-13T...",
  "totals": {"files": 0, "bytes": 0},
  "groups": [{"name": "notes", "files": 0, "bytes": 0}],
  "reproducible_tool": [
    {"path": "...", "type": "file", "bytes": 0, "mtime_ns": 0, "mode": "0o644", "sha256": "..."},
    {"path": "...", "type": "symlink", "target": "../original/x.pdf", "bytes": 0}
  ]
}
```

`manifests/corpus.json` records aggregate PDF/page/text counts only. Per-PDF
details are available only with the explicit, private `--include-paths` option.
`manifests/models.json` pins the Hugging Face revision and file checksums.
`manifests/environment.json` records portable interpreter and package data,
without host usernames or absolute paths.

Regenerate:

```bash
source 00_orchestrator/runtime.sh
python scripts/assets_manifest.py generate --hash
python scripts/assets_manifest.py generate --public-summary --no-hash
python scripts/corpus_manifest.py --no-hash
# Private, local-only detail (do not commit or publish):
python scripts/corpus_manifest.py --include-paths --output manifests/corpus.private.json
python scripts/fetch_models.py --record
python scripts/environment_report.py
```

Verify:

```bash
python scripts/assets_manifest.py verify --output manifests/local_assets.private.json --hash
```

## Transfer options

### 1. Streaming tar parts (portable, resumable by re-running)

```bash
# ~13 GB without the VM
bash scripts/pack_local_assets.sh --output /media/usb/sam_win

# ~48 GB including the VM
bash scripts/pack_local_assets.sh --include-vm --output /media/usb/sam_win
```

Parts are 95 MB, below the 99 MB target. `SHA256SUMS` covers every part. Restore:

```bash
bash /media/usb/sam_win/restore_local_assets.sh /path/to/samsung_notes
```

### 2. rsync (fastest on a LAN, preserves hard links)

```bash
rsync -aH --info=progress2 \
  --exclude='.git' --exclude='05_environments_and_venvs/python_ocr_env' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='page_cache' \
  old-host:/path/to/sam_win/ ./sam_win/
```

### 3. restic / Borg (encrypted, incremental, good for cloud)

```bash
restic init --repo /media/backup/sam_win-restic
restic -r /media/backup/sam_win-restic backup /path/to/sam_win \
  --exclude '05_environments_and_venvs/python_ocr_env' \
  --exclude '**/__pycache__' --exclude '**/page_cache'
restic -r /media/backup/sam_win-restic restore latest --target /restore
```

Never upload the notes corpus to a public bucket. If cloud is the only option,
encrypt client-side (restic/Borg/age) and keep the key separate.

### 4. VM disk only

```bash
qemu-img convert -O qcow2 -c 01_vm_windows_sync/vm/notes.qcow2 \
  /media/usb/notes-compressed.qcow2
```

Compression only helps if the guest has free space; a nearly full disk may not
shrink much. Copy `OVMF_VARS.fd` alongside the disk.

## What can be rebuilt instead of transferred

| Artifact | Rebuild command | Notes |
|---|---|---|
| `data/local_index.sqlite` | `rebuild_index_offline.sh` | 5–15 min, deterministic from PDFs |
| `data/index.json` / Chroma | Legacy path | Preserved for history; not required |
| OCR visual cache | `backfill_searchable_text.py` | Only useful with OCR enabled |
| Page cache | Incremental exporter | Regenerated per changed page |
| Python environment | `bootstrap.sh` | Never copy a venv across machines |
| Model weights | `fetch_models.py` | 133 MB, checksum-verified |

## Retention and safety

- `notes/original/` is immutable during OCR/derivation. Back up before repair.
- `preserved_originals/` holds the previous inode/hash of any replaced export.
- Never delete a PDF just because the index marks it `empty`; empty is a
  content property, not a defect.
- Keep at least two copies of the corpus (local + encrypted external) and one
  copy of the VM disk if you cannot rebuild the Samsung session.
- The VM disk contains an authenticated Samsung session; treat it like a
  password.
