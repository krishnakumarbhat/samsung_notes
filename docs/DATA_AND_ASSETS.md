# Data and assets

Git is used for **code and evidence**, not for the note corpus. This page records
what exists locally, why it is excluded, and how to move it safely.

## Inventory (reference machine, 2026-11-21)

| Group | Path | Files | Size | In Git? | Restore |
|---|---|---:|---:|---|---|
| Windows VM | `01_vm_windows_sync/vm/` | 4 | 34.79 GB | No | Copy disk or rebuild |
| Exported notes | `02_extracted_notes_pdf/notes/compressed/` | local-only | 4.4 GB after cleanup | No | Bundle / rsync |
| Export history | `02_extracted_notes_pdf/ai_agend_history/` | local-only | 531 MB after cleanup | Mostly no | Bundle / rsync |
| BGE-small model | `05_environments_and_venvs/models/` | 11 | 127 MB | Config only | `fetch_models.py` |
| App data | `04_learning_recall_app/my_notes_graph/data/` | local-only | ~303 MB | No (indexes/DBs) | Bundle or rebuild |
| Shared CPU Python packages | `../python_libs/` | local-only | 6.8 GB | No | Reinstall from requirements |
| Shared GPU/CUDA Python packages | `../python_libs_gpu/` | local-only | 9.0 GB | No | Reinstall matching driver wheels |
| Archived legacy history | `../sam_notes/history_backup/` | local-only | 175 MB | No | Restore individual entries from `manifest.json` |
| Archived package leftovers | `../sam_notes/archived_envs/` | local-only | 585 MB | No | Restore only if explicitly needed |
| Run scratch | `../sam_notes/_run_tmp_<pid>/` | transient | removed on exit | No | Regenerated per run |

The reference inventory is a snapshot, not a promise that payloads are present
in a fresh clone. The public manifests deliberately omit per-note filenames,
paths, hashes, titles and text. A detailed inventory is generated locally into
an ignored `*.private.json` file and can travel only inside the encrypted
personal transfer bundle.

## Workspace disk cap

`03_rtx_compression_ocr/derive_corpus.py` refuses to publish a derived PDF once
`du -s` of the workspace reaches 55 GB. The UUID-first reconciler uses the
read-only guest inventory as the folder source of truth and keeps the canonical
compressed PDF tree aligned with it:

| Tree | Location | Why |
|---|---|---|
| `02_extracted_notes_pdf/notes/compressed/` | inside the workspace | canonical standalone PDFs; read by Recall indexer and app |
| `02_extracted_notes_pdf/notes/original/` | legacy-only; do not populate | old source copies are preserved under history if encountered |
| `../sam_notes/_run_tmp_<pid>/` | outside `sam_win` | transient WDoc/render/OCR/derive staging; removed by the owning run |
| `02_extracted_notes_pdf/ai_agend_history/page_cache/` | inside the workspace | bounded content-addressed page cache with LRU/orphan pruning |

The reconciler runs after guest sync quiescence and before export. It reads
`Storage.sqlite` through SQLite `mode=ro` plus `PRAGMA query_only=ON`; renames
and moves are keyed by immutable UUID, not folder names. Compressed-only mode
is intentional: `notes/compressed/` is the sole canonical PDF store, and the
reconciler never creates or populates `notes/original/`. Existing legacy files
found there are archived safely. Unmapped PDFs, recycled-note PDFs and
conflicting duplicates are archived rather than deleted. Retired
`preserved_originals/`, `repair_backups/`, old reports/logs and orphaned page
cache hashes are preserved outside the workspace under
`../sam_notes/history_backup/manifest.json`. A same-UUID folder move updates
paths without rendering, OCR or re-embedding. Raw WDoc bundles and candidate PDFs are staged under the
per-run sibling `../sam_notes/_run_tmp_<pid>/` tree and removed on exit.

Shared Python packages live only in sibling `../python_libs_gpu/` (GPU/CUDA,
Paddle and Torch) and `../python_libs/` (CPU/application packages). Runtime
staging and disposable Paddle, Matplotlib and CUDA caches use a per-run
`../sam_notes/_run_tmp_<pid>/` directory and are removed on exit.
`SN_PAGE_CACHE_DIR` intentionally remains in the workspace so its 256 MiB
budget and LRU pruning can be audited.

The orchestrator (`00_orchestrator/sync_and_export_notes.sh`) checks the 55 GB
ceiling and a 15 GiB free-space floor before any VM boot, and again after guest
sync. The background monitor started by `01_vm_windows_sync/run-notes.sh start`
runs every **30 seconds** with two complementary checks:

| Check | Measurement | Threshold | When it shuts down the VM |
|---|---|---|---|
| `vm-qcow2` | allocated bytes of `vm/notes.qcow2` only | 54 GB (early warning at 52 GB) | only when no pipeline phase is declared; advisory during `sync`, deferred during `export` |
| `workspace` | `du -s` of the whole checkout | 55 GB hard cap (warning at 52 GB), or < 15 GiB free | during `sync` and when unowned; **deferred during `export`** |

The two thresholds are not contradictory: the qcow2 is a subset of the
workspace, so its lower 54 GB value is an early, deferrable warning; the 55 GB
workspace value is the hard cap. A failed or unparseable `du`/`df` reading is
never treated as zero usage — it fails closed (shutdown when unowned or in
`sync`, deferred only during `export`).

The orchestrator declares a phase in `temp/export_active.flag`
(`"<live PID> sync"` before guest sync, then `"<live PID> export"` before the
exporter, removed afterwards and on every exit). A stale flag with a dead PID
is ignored. During `export`, transient staging is external, but the **exporter
guard** remains authoritative: `derive_corpus.guard` checks workspace
allocation, the 15 GiB volume floor and the per-note reserve before each
stage/derive and refuses unsafe publication. Shutdown is always
graceful ACPI via the QEMU monitor socket — never a force-kill. Writes performed
inside the Windows guest are not hard-capped by the monitor; the 55 GB guarantee
for staged/derived data is enforced by the exporter guard.

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
  --exclude='.git' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='page_cache' \
  old-host:/path/to/sam_win/ ./sam_win/
```

### 3. restic / Borg (encrypted, incremental, good for cloud)

```bash
restic init --repo /media/backup/sam_win-restic
restic -r /media/backup/sam_win-restic backup /path/to/sam_win \
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

- Compressed-only mode never populates `notes/original/`; any legacy source PDF
  found there is archived rather than duplicated or deleted.
- `ai_agend_history/preserved_originals/` holds differing prior exports and
  conflict copies; `unmapped_orphans/` and `recycled_notes/` preserve other
  unmatched/deleted PDFs.
- Never delete a PDF just because the index marks it `empty`; empty is a
  content property, not a defect.
- Keep the Samsung Notes VM as the source until standalone PDFs have been
  validated. The exporter treats the VM's raw note database as read-only.
- The VM disk contains an authenticated Samsung session; treat it like a
  password.
