# Replication runbook

Goal: move from a fresh OS install to the same working Samsung Notes pipeline
with the fewest manual steps. There are two paths:

| Situation | Fastest path |
|---|---|
| New OS on the **same machine**, data disk intact | Reinstall OS, clone the repo, bootstrap, point it at the existing data |
| New machine / new disk | Clone the repo, restore the transfer bundle, recreate or copy the VM |
| Code review / CI only | Clone and run `scripts/verify_repo.py` and the meta-tests |

Everything that makes the workspace unique is either **in Git** (code, configs,
metadata, evidence) or **in one bundle** (notes, history, model, app data, VM).

---

## 0. What is and is not in Git

| Asset | In Git? | Why | Restore |
|---|---|---|---|
| Source code, scripts, tests, docs | Yes | Small and reviewable | `git clone` |
| Corpus metadata + SHA-256 | Yes (`manifests/`) | Proves the restored corpus | `assets_manifest.py verify` |
| Exported PDFs (`02_.../notes/`) | No | 12 GB, personal notes | Bundle / rsync |
| VM disk (`01_.../vm/notes.qcow2`) | No | 34 GB, Windows licence | Copy or rebuild |
| Model weights (133 MB) | No | Re-downloadable, >99 MB | `fetch_models.py` |
| Python packages (`../python_libs`, `../python_libs_gpu`) | No | Reinstallable, multi-GB | `bootstrap.sh` |
| `.env` secrets | No | Credentials | Enter manually |

The GitHub hard limit is 100 MB per file. This repository enforces a **99 MB**
ceiling and refuses to track `.env` files.

---

## 1. Prerequisites

### Linux (reference platform)

Tested on Ubuntu 22.04/24.04 with Python 3.10 and an NVIDIA RTX 3050 4 GB.

```bash
sudo apt update
sudo apt install -y \
  git curl python3 python3-venv python3-pip \
  qemu-system-x86 qemu-utils ovmf socat sshpass \
  nvidia-driver-535  # or the driver recommended by `ubuntu-drivers devices`
```

Optional but useful: `rsync`, `zstd`, `restic`, `shellcheck`.

### macOS

The dashboard and the offline indexer run natively; the VM and page exporter are
Linux-only. Use a Linux VM/WSL2 for those stages.

```bash
brew install python@3.12 git rsync
```

### Windows

Run the pipeline inside **WSL2 + Ubuntu**. Native Windows can run the dashboard,
but `fcntl`, `flock`, `sshpass` and the QEMU/KVM path assume Linux.

```powershell
wsl --install -d Ubuntu-24.04
```

Then follow the Linux steps inside WSL. Keep the repository on the Linux
filesystem (`~/sam_win`), not under `/mnt/c`, to avoid permission and
performance problems.

### Hardware

| Component | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16 GB (6 GB is assigned to the VM) |
| Disk | 80 GB free | 120 GB SSD/NVMe |
| GPU | CPU-only OCR works | NVIDIA 4 GB+ for PaddleOCR |
| CPU | 2 cores | 4+ cores |

The documented workspace cap is **55 GB**, with a **15 GiB free-space floor**.

---

## 2. Clone and bootstrap

```bash
git clone https://github.com/krishnakumarbhat/samsung_notes.git
cd samsung_notes

# Inspect first: no side effects
python3 scripts/doctor.py --mode core
bash scripts/bootstrap.sh --check-only --all --models

# App + embeddings + exporter + tests (not OCR)
bash scripts/bootstrap.sh --app --semantic --export --dev

# Optional: OCR stack (CPU wheel)
bash scripts/bootstrap.sh --ocr

# Optional: GPU wheel (install the CUDA runtime first)
bash scripts/bootstrap.sh --ocr-gpu

# Pinned 133 MB embedding model
bash scripts/bootstrap.sh --models
```

The host's existing Python interpreter is used by default. If a custom
interpreter is required, set `SAM_PYTHON` to an existing executable; package
imports still come only from the shared CPU/GPU libraries.

Activate the runtime resolver:

```bash
source 00_orchestrator/runtime.sh
```

`runtime.sh` loads packages only from `../python_libs_gpu` and
`../python_libs`; transient run scratch is created below `../sam_notes/` and
removed on exit.

---

## 3. Secrets and environment files

The repository ships blank examples:

```bash
cp .env.example .env
cp 02_extracted_notes_pdf/.env.example 02_extracted_notes_pdf/.env
chmod 600 .env 02_extracted_notes_pdf/.env
```

Fill only what you need:

| File | Key | Purpose |
|---|---|---|
| `.env` | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Tutor, exam grading, question factory |
| `.env` | `GEMINI_*_MODEL`, retry/budget values | Model chain and bounded backoff |
| `02_extracted_notes_pdf/.env` | `SAMSUNG_NOTES_PASSWORD` | SSH into the Windows guest |
| `~/.config/sam_win/.env` | same Gemini keys | Preferred 0600 secret location |

The app reads `~/.config/sam_win/.env` **first** and ignores blank values, so a
sanitized repository `.env` cannot mask the real key. Override the path with
`SAM_ENV_FILE`.

Never commit any of these files. `scripts/verify_repo.py` fails if one is
tracked or if a key pattern appears in tracked text.

---

## 4. Restore the local data

### Option A — bundle (works across machines)

On the old machine:

```bash
# Notes, history, model, app data (VM excluded by default, ~13 GB)
bash scripts/pack_local_assets.sh --output /media/usb/sam_win

# Include the 34 GB VM if you are moving to identical hardware
bash scripts/pack_local_assets.sh --include-vm --output /media/usb/sam_win
```

The script streams a tar into 95 MB parts and writes `SHA256SUMS`,
`RESTORE.txt` and `local_assets.json`. It does not need a second 50 GB copy.

On the new machine:

```bash
bash /media/usb/sam_win/restore_local_assets.sh "$PWD"
python3 scripts/assets_manifest.py verify --hash
```

### Option B — rsync (fastest on the same LAN)

```bash
rsync -aH --info=progress2 \
  --exclude='.git' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='page_cache' \
  old-host:/path/to/sam_win/ ./sam_win/
```

`-H` preserves the hard links used by the compressed tree. Then copy the VM disk
separately:

```bash
rsync -aH --info=progress2 old-host:/path/to/sam_win/01_vm_windows_sync/vm/notes.qcow2 \
  01_vm_windows_sync/vm/
```

### Option C — rebuild the corpus from Samsung Notes

If neither the PDFs nor the VM survived, follow
[`01_vm_windows_sync/README.md`](01_vm_windows_sync/README.md), sign in to
Samsung Notes and run the incremental exporter. Expect several hours and respect
the Samsung cloud quota.

---

## 5. The Windows VM

### Copy the existing disk (fastest)

```bash
mkdir -p 01_vm_windows_sync/vm
rsync -aH notes.qcow2 OVMF_VARS.fd 01_vm_windows_sync/vm/
touch 01_vm_windows_sync/vm/.installed
bash 01_vm_windows_sync/run-notes.sh start
bash 01_vm_windows_sync/run-notes.sh vnc   # watch on 127.0.0.1:5901
```

The disk contains the Samsung account session and the `notes` user. Treat it as
a credential: keep it encrypted at rest.

### Rebuild from a Windows ISO

```bash
export WINDOWS_ISO=/path/to/Win11_24H2_English_x64.iso
bash 01_vm_windows_sync/build-vm.sh
bash 01_vm_windows_sync/run-notes.sh start
bash 01_vm_windows_sync/run-notes.sh vnc
```

Manual steps inside Windows are unavoidable and intentional:

1. Install Windows 11; create the user `notes`.
2. Install OpenSSH Server and enable it (`Add-WindowsCapability`).
3. Install **Samsung Notes** from the Microsoft Store and sign in.
4. Review and accept Samsung's Terms/Privacy in the UI (the automation never
   accepts consent on your behalf).
5. Keep the session unlocked, disable sleep and Fast Startup.
6. Provide Python at `C:\py\python.exe` for the inventory helper.
7. After the first successful sync, mark the VM installed:
   `touch 01_vm_windows_sync/vm/.installed`.

Verify:

```bash
bash 01_vm_windows_sync/run-notes.sh status
bash 01_vm_windows_sync/guestsetup/sync_now.sh
```

---

## 6. GPU / OCR (optional)

```bash
nvidia-smi
bash scripts/bootstrap.sh --ocr-gpu
bash 03_rtx_compression_ocr/run_ocr.sh --probe

# Prove on 2–3 copies before touching the corpus
bash 03_rtx_compression_ocr/run_ocr.sh /path/to/sample.pdf --device gpu \
  --output-dir /tmp/ocr-test
```

The 4 GB profile detects mobile GPUs and retries with smaller raster sizes on
CUDA OOM. OCR is serialized by `gpu_ocr.lock`; CPU embeddings do not compete for
VRAM.

---

## 7. Rebuild the retrieval index

```bash
bash 04_learning_recall_app/my_notes_graph/rebuild_index_offline.sh
```

This reads `02_extracted_notes_pdf/notes/compressed` and writes
`data/local_index.sqlite`. It is fully offline, makes no HTTP embedding calls,
and distinguishes `searchable`, `empty` and `unreadable` sources.

Expected on the reference corpus: 274 processed, 248 searchable, 26 empty,
0 unreadable, ~10,541 chunks.

---

## 8. Verification matrix

Run these in order after a restore:

```bash
python3 scripts/doctor.py --mode all
python3 scripts/assets_manifest.py verify --hash
python3 scripts/verify_repo.py
python3 scripts/run_tests.py --json-out /tmp/sam_win_tests.json
bash 04_learning_recall_app/my_notes_graph/run_app.sh
curl -fsS http://127.0.0.1:8787/api/health
bash 00_orchestrator/sync_and_export_notes.sh --dry-run
```

Expected:

| Check | Expected |
|---|---|
| `doctor --mode all` | 0 failures; warnings only for optional assets |
| `assets_manifest verify --hash` | every restored file matches |
| `verify_repo` | 0 failures |
| `run_tests.py` | app, pipeline, OCR, guestsetup suites pass |
| `/api/health` | HTTP 200, `chunks` matching your corpus |
| sync dry-run | exit 0, `changed=0` after a quiescent sync |

If a check fails, use [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

---

## 9. Time and space estimates

| Step | Time | Disk |
|---|---|---|
| Clone + bootstrap (app/semantic/export/dev) | 5–15 min | ~3 GB |
| Model download | 1–3 min | 133 MB |
| OCR stack (CPU or GPU wheels) | 5–20 min | 3–8 GB |
| Bundle restore (13 GB, USB 3) | 5–15 min | 13 GB |
| VM copy (34 GB) | 10–40 min | 34 GB |
| Windows rebuild + Samsung sign-in | 45–120 min | 34 GB |
| Full index rebuild | 5–15 min | ~300 MB |
| Full test suite | ~90 s | — |

---

## 10. What cannot be automated

- Samsung account sign-in and Terms acceptance.
- Windows licence activation.
- The initial Samsung Notes cloud download (quota and network dependent).
- Recovering a lost VM disk without a backup.
- Rebuilding `tools/sdocx-cli` from source: the prebuilt Linux x86-64 binary is
  included with a SHA-256, but its Rust source is not part of this repository.
  Keep a copy of the binary and verify `sha256sum tools/sdocx-cli` before use.

---

## 11. Move the session to a new working directory

If you clone the repository somewhere else and want the current agent session to
follow it, use `session_move` in OpenCode or start a new session in the clone.
All scripts discover the repository root from their own path; only the VM disk
and `.env` secrets need explicit copying.
