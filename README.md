# Samsung Notes → searchable study workspace

A reproducible, local-first pipeline that exports Samsung Notes from a Windows
VM, repairs legacy PDF exports, adds a searchable OCR layer, indexes the whole
corpus offline, and serves a Socratic study dashboard with FSRS scheduling.

The repository contains **code, configuration, sanitized evidence and
aggregate metadata**. Personal note PDFs, the VM disk and model weights stay
local; model/tool checksums and restore instructions are committed. See
[`REPLICATION.md`](REPLICATION.md) to rebuild everything on a new OS.

[![CI](https://github.com/krishnakumarbhat/samsung_notes/actions/workflows/ci.yml/badge.svg)](https://github.com/krishnakumarbhat/samsung_notes/actions/workflows/ci.yml)

## Verified state

Historical end-to-end Stage 3 evidence, captured 2026-10-20 and indexed in
[`docs/EVIDENCE_INDEX.md`](docs/EVIDENCE_INDEX.md):

| Metric | Value |
|---|---:|
| Physical exported PDFs | **274 / 274 indexed** (historical run) |
| Searchable PDFs | **248** (26 genuinely empty, 0 unreadable; historical run) |
| Retrieval chunks | **10,541** (historical run) |
| Corpus pages inspected | **8,552** (historical run) |
| Defect audit | 214 black-canvas files / 3,980 black pages → **0 after repair** |
| Embedded-PDF repair | 2 notes, **54/54 and 32/32 pages pixel-exact** |
| Full test run | **230 tests passed** across 4 suites (2026-11-13) |
| Dashboard | `http://127.0.0.1:8787` (bind host configurable) |

The public, aggregate-only historical summary lives in
[`00_orchestrator/STAGE3_PUBLIC_SUMMARY.json`](00_orchestrator/STAGE3_PUBLIC_SUMMARY.json).
Detailed machine reports and operational exports are intentionally local-only;
[`docs/EVIDENCE_INDEX.md`](docs/EVIDENCE_INDEX.md) maps public claims to safe
evidence or reproducible commands.

## Quick start (this machine)

```bash
# 1. Check the machine without changing anything
python3 scripts/doctor.py --mode all

# 2. Create the Python environment outside the repository (~2 GB for app+semantic)
bash scripts/bootstrap.sh --app --semantic --export --dev

# 3. Download the pinned embedding model (133 MB)
bash scripts/bootstrap.sh --models

# 4. Run the four test suites with the correct PYTHONPATH
python3 scripts/run_tests.py

# 5. Start the dashboard
bash 04_learning_recall_app/my_notes_graph/run_app.sh
```

Open <http://127.0.0.1:8787>. Stop it with
`bash 04_learning_recall_app/my_notes_graph/run_app.sh --stop`.

The dashboard works with the **lexical fallback** if the semantic model is
missing; retrieval quality improves once `--semantic` and `--models` are done.

## One-click sync (existing Linux host)

```bash
bash 00_orchestrator/sync_and_export_notes.sh --dry-run
bash 00_orchestrator/sync_and_export_notes.sh
```

The runner owns a workspace lock, starts only its own VM, syncs Samsung Notes in
the interactive guest session, exports changed pages, OCRs new PDFs, refreshes
the local index and starts the dashboard. A failed stage returns nonzero and
preserves the previous export.

## Repository layout

| Path | Responsibility |
|---|---|
| [`00_orchestrator/`](00_orchestrator/) | One-click sync/export/OCR/index runner, runtime resolver, sanitized aggregate evidence |
| [`01_vm_windows_sync/`](01_vm_windows_sync/) | QEMU Windows VM, Samsung Notes guest automation, sync quiescence checks |
| [`02_extracted_notes_pdf/`](02_extracted_notes_pdf/) | Incremental exporter, page deltas, PDF repair, export state |
| [`03_rtx_compression_ocr/`](03_rtx_compression_ocr/) | PaddleOCR bridge, invisible text layer, lossless compression |
| [`04_learning_recall_app/`](04_learning_recall_app/) | FastAPI dashboard, FSRS, quiz bank, local hybrid retrieval |
| [`05_environments_and_venvs/`](05_environments_and_venvs/) | Environment notes, model metadata, reinstall recipes |
| [`scripts/`](scripts/) | Bootstrap, doctor, test runner, manifests, transfer bundle, hygiene gate |
| [`manifests/`](manifests/) | Aggregate corpus/asset metadata, model pin and portable environment record |
| [`docs/`](docs/) | Architecture, research decisions, assets, evidence index, troubleshooting |

## Tests

```bash
python3 scripts/run_tests.py                  # all suites (~90 s)
python3 scripts/run_tests.py --fast           # skip the slow index/e2e tests
python3 scripts/run_tests.py --suite app      # one suite
python3 scripts/verify_repo.py                # pre-push hygiene gate
```

The runner exists because a bare `pytest` picks up an incompatible global
`pytest_asyncio` plugin on the shared package trees. It disables plugin
autoloading, sets the cooperative `PYTHONPATH`, and runs each suite from its own
directory.

## Data policy

- **Never commit** `01_vm_windows_sync/vm/*.qcow2`, `02_extracted_notes_pdf/notes/**`,
  `05_environments_and_venvs/models/**/*.safetensors`, the sibling
  `../python_libs/` or `../python_libs_gpu/` package trees, or any `.env` file.
- GitHub rejects individual files over 100 MB; this repository enforces a
  **99 MB** tracked-file ceiling and warns above 50 MB.
- The note corpus is personal data. Keep transfer bundles on encrypted media and
  do not paste note text into public issues.
- Credentials live in `~/.config/sam_win/.env` (mode 0600). Tracked
  `.env.example` files contain placeholders only.

## Reproduce on a new machine

Read [`REPLICATION.md`](REPLICATION.md). The short version:

```bash
git clone https://github.com/krishnakumarbhat/samsung_notes.git
cd samsung_notes
bash scripts/bootstrap.sh --app --semantic --export --dev --models
python3 scripts/doctor.py --mode all
bash /path/to/bundle/restore_local_assets.sh "$PWD"
python3 scripts/assets_manifest.py verify --hash
python3 scripts/run_tests.py
```

The VM can be copied (fastest on identical hardware) or rebuilt with
[`01_vm_windows_sync/build-vm.sh`](01_vm_windows_sync/build-vm.sh) and the guest
checklist. The Windows ISO, Samsung account and Samsung Notes terms acceptance
are manual by design.

## Documentation

- [`REPLICATION.md`](REPLICATION.md) — fresh-OS and new-machine runbook.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — data flow and component map.
- [`docs/RESEARCH.md`](docs/RESEARCH.md) — retrieval, PDF-fidelity, OCR and
  adaptive-learning decisions with measured results and sources.
- [`docs/DATA_AND_ASSETS.md`](docs/DATA_AND_ASSETS.md) — asset inventory,
  exclusions, checksums and transfer options.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — known failures and fixes.
- [`docs/EVIDENCE_INDEX.md`](docs/EVIDENCE_INDEX.md) — public evidence and reproducible checks.

## Known limitations

- 26 PDFs have no selectable text and are honestly labelled `empty`; they are
  not counted as searchable.
- 46 compressed sources have no derived-manifest row, but all 274 physical PDFs
  are indexed.
- One `Probability and Statistics` note stays unsupported because ink overlays
  embedded PDF paper; its original is preserved.
- Lossless compression saves only ~1.9–2.4%, far short of the observed 32%
  cloud-quota reduction needed. No lossy or destructive cloud workflow is
  automated.
- The prebuilt `tools/sdocx-cli` is Linux x86-64. Windows users should run the
  exporter inside WSL2.

## Safety

The pipeline never writes to Samsung Notes production databases: guest SQLite is
opened with `mode=ro` and `PRAGMA query_only=ON`. OCR text is written only to the
compressed derivative, never through a placeholder into an original. Every
publication is atomic and gated by page-count, syntax and render checks. See
[`SECURITY.md`](SECURITY.md).
