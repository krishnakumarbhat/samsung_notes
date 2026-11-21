# Troubleshooting

Start with:

```bash
python3 scripts/doctor.py --mode all
python3 scripts/run_tests.py --verbose
```

## Python and pytest

### `ImportError: cannot import name 'FixtureDef' from 'pytest'`

A global `pytest_asyncio` plugin is incompatible with the selected `pytest`.
This happens when `PYTHONPATH` mixes the OCR site-packages with user site
packages.

```bash
# Use the runner, which disables plugin autoloading:
python3 scripts/run_tests.py

# Manual equivalent:
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

### `ModuleNotFoundError: langsmith` during pytest startup

Same root cause: an unrelated plugin entry point. The runner's
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` avoids it. Do not `pip install langsmith`
just to silence a plugin you do not use.

### `ModuleNotFoundError: fsrs` / `pikepdf` / `fastapi`

The interpreter does not see the shared package trees. Source the resolver:

```bash
export SAM_PYTHON="$HOME/.cache/sam_win/venv/bin/python3"
source 00_orchestrator/runtime.sh
python -c 'import fsrs, pikepdf, fastapi; print("ok")'
```

If the packages are genuinely absent, `bash scripts/bootstrap.sh --app --export`.

### `AttributeError: module 'fitz' has no attribute 'open'`

The unrelated PyPI package `fitz` is shadowing PyMuPDF.

```bash
python3 -m pip uninstall -y fitz frontend
python3 -m pip install --upgrade PyMuPDF==1.26.7
```

`03_rtx_compression_ocr/pymupdf_compat.py` also detects this and prints the fix.

## Native libraries

### `ImportError: libopenblasp-r0-...so: cannot open shared object file`

The OCR/NumPy wheel cannot find its bundled BLAS library. Use the OCR launcher,
which sets `LD_LIBRARY_PATH` for the CUDA and OpenMP/BLAS directories:

```bash
bash 03_rtx_compression_ocr/run_ocr.sh --probe
```

If it persists, reinstall the CPU numeric stack into the bootstrapped venv:

```bash
bash scripts/bootstrap.sh --ocr
```

### `RuntimeError: Directory 'static/' does not exist`

A test or app was started from the wrong directory. Run the app from its own
folder (the launcher does this):

```bash
bash 04_learning_recall_app/my_notes_graph/run_app.sh
```

The test runner sets each suite's working directory explicitly.

### Paddle GPU OOM on the 4 GB RTX 3050

The pipeline detects mobile GPUs and retries with smaller raster sizes. If a
single page still fails:

```bash
bash 03_rtx_compression_ocr/run_ocr.sh file.pdf --device cpu
```

CPU OCR is slower but keeps the original safe. Never disable the page-count and
render checks to force a result.

## Model and index

### Semantic model missing / `HF_HUB_OFFLINE=1` warning

```bash
python3 scripts/fetch_models.py --check
python3 scripts/fetch_models.py
```

The dashboard still works with the lexical-hashing fallback. Check
`/api/health` for the active embedding mode.

### Index rebuild is slow or refuses to start

- It requires 15 GiB free space.
- It caps the index/cache at 300 MiB.
- It reads every PDF; 5–15 minutes is normal.
- A stale lock (`data/local_index.lock`) only blocks concurrency, not a crashed
  process; inspect before deleting.

## VM and export

### `/dev/kvm: Permission denied`

```bash
sudo usermod -aG kvm "$USER"
newgrp kvm
```

Log out/in if needed. Without KVM the VM still boots under TCG but is very slow.

### `Missing SAMSUNG_NOTES_PASSWORD`

Store the guest credential on the native Linux filesystem (not the NTFS/FUSE
workspace, where `chmod 600` may not persist):

```bash
install -d -m 700 ~/.config/sam_win
touch ~/.config/sam_win/.env
chmod 600 ~/.config/sam_win/.env
# Add SAMSUNG_NOTES_PASSWORD=<guest password> with a secure editor; do not echo it.
```

The guest sync and export tools read `~/.config/sam_win/.env` first, falling
back to `02_extracted_notes_pdf/.env` only when the native file has no guest
credential key. The password belongs to the Windows user `notes`, not the
Samsung account. It is passed through `SSHPASS`, never argv or logs. Rotate it
if it has not changed since the earlier sync failures.

### `Windows VM SSH did not become ready within 120 seconds`

1. `bash 01_vm_windows_sync/run-notes.sh status`
2. `bash 01_vm_windows_sync/run-notes.sh vnc` and inspect the screen.
3. Confirm OpenSSH Server is running and `notes@127.0.0.1:2222` is reachable.
4. Check that Windows is not sleeping or showing a lock screen.
5. If Samsung Notes shows Terms/Privacy, accept it manually, then re-run.

### `sdocx-cli missing`

```bash
ls -l tools/sdocx-cli
sha256sum tools/sdocx-cli   # compare with manifests/local_assets.json's reproducible_tool entry
export SAM_SDOCX_CLI="$PWD/tools/sdocx-cli"
```

The binary is Linux x86-64. Use WSL2 on Windows. If the checksum does not match,
do not run it; restore the trusted copy from your backup.

### `Disk guard refused`

The exporter refuses below 15 GiB free or above the 55 GB workspace cap. Free
space does **not** mean deleting notes. Options:

- Move the VM disk to another volume and symlink it.
- Archive `preserved_originals/` to external storage.
- Remove regenerable caches (`page_cache/`, `ocr_visual_cache/`).
- Raise the documented cap deliberately; never bypass the free-space floor.

### Folder move causes a full export

The Phase 0 UUID tree reconciler runs after guest sync quiescence. It reads the
guest `Storage.sqlite` through `mode=ro` and `PRAGMA query_only=ON`, then matches
local PDFs by UUID, manifest path and verified digest. A folder rename/move with
an unchanged UUID and content should only update local paths; it must not render,
OCR or re-embed. Review its private report under
`02_extracted_notes_pdf/ai_agend_history/reconcile_tree.*.json`. Conflicts,
deleted notes and unknown PDFs are archived, not silently deleted. Never edit
the guest database to repair local paths. The current storage mode is
compressed-only: `notes/compressed/` is the canonical store; do not create or
populate `notes/original/`. Existing legacy originals are archived safely.

### OCR imports fail after moving the environment

Python packages are loaded only from sibling `../python_libs_gpu/` and
`../python_libs/`. Source the runtime resolver before manual commands:

```bash
source 00_orchestrator/runtime.sh
python3 -c 'import fitz, pikepdf, paddleocr; print("runtime OK")'
```

Transient staging, Paddle, Matplotlib, CUDA and OCR-visual caches use a
per-process `../sam_notes/_run_tmp_<pid>/` directory that is removed on exit;
the page-delta cache remains under `ai_agend_history/page_cache/` and is
automatically pruned to its configured limit.

### Sync says `IsDirty` never reaches zero

- Keep the interactive Windows session unlocked.
- Open Samsung Notes and let the spinner finish.
- Check network/proxy in the guest.
- A Samsung service outage can hold the dirty flag; do not loop the click
  automation indefinitely. Stop and retry later.

### A note exports blank or with missing paper

- Check the local, ignored repair report under `02_extracted_notes_pdf/ai_agend_history/` for that UUID.
- Run the read-only audit first:
  `python3 02_extracted_notes_pdf/audit_and_repair_blanks.py`.
- `--repair` is opt-in and only publishes source-verified ink repairs.
- Embedded-PDF paper with ink overlay is intentionally unsupported; the original
  and placeholder are preserved.

## Dashboard

### Port 8787 already in use

```bash
bash 04_learning_recall_app/my_notes_graph/run_app.sh --stop
ss -ltnp | grep 8787
```

The launcher refuses to start a second instance if `/api/health` responds.

### PDF drawer shows `net::ERR_ABORTED` in Chrome

Chrome aborts the first iframe Range request before its native viewer retries.
An independent `Range` GET returns HTTP 206 and the page renders. This is a
benign browser behaviour recorded in the Stage 3 QA; it is not a server failure.

### Gemini 401/403

Authentication failures stop retries by design. Check the key in
`~/.config/sam_win/.env` (preferred) or `.env`. Model names are identifiers, not
proof of access; use a model your key can call.

### Gemini 429/503

The client applies bounded exponential backoff with jitter and then tries the
configured fallback. If both fail, the UI shows the failure; it does not
fabricate a quiz.

## Git and transfer

### `pack_local_assets.sh` runs out of space

The script streams tar into `split`, so it needs only the output space. If you
are packing to a USB stick, check `df -h` first. Use `--notes-only` for the
smallest useful bundle.

### `sha256sum -c` fails after a restore

Re-copy the affected part. Do not extract a bundle with a failed checksum.
`restore_local_assets.sh` verifies every part before writing.

### CRLF or executable-bit problems on Windows

`.gitattributes` normalizes text to LF and marks binaries. Enable
`core.autocrlf=false` in WSL. Executable bits matter for `.sh`; run
`chmod +x scripts/*.sh` if a ZIP transfer stripped them.

### A tracked `.env` was committed

Remove it from the index immediately, rotate the credential, and rewrite history
only if the repository is private. A public push is irreversible: treat the
secret as compromised.

```bash
git rm --cached .env 02_extracted_notes_pdf/.env
git commit -m "Remove tracked environment files"
```
