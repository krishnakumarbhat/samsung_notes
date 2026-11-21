# Architecture

The system is a one-way pipeline: Samsung Notes remains the source of truth, the
Windows VM is a read-only extraction surface, and every derived artifact is
rebuildable from the exports.

```mermaid
flowchart LR
    subgraph Guest["Windows 11 VM (QEMU/KVM)"]
        SN[Samsung Notes<br/>Store app]
        DB[(Storage.sqlite<br/>read-only)]
        WDOC[wdoc page store<br/>read-only]
        SSH[OpenSSH + notes user]
    end

    subgraph Host["Linux host"]
        SYNC[00_orchestrator<br/>sync_and_export_notes.sh]
        REC[Phase 0 UUID tree reconciler]
        EXP[02 incremental exporter<br/>sdocx-cli + page_delta]
        STAGE[(external sam_notes/_run_tmp_<pid>)]
        REP[UUID tree reconcile<br/>template recovery<br/>light-canvas normalise]
        OCR[03 PaddleOCR bridge<br/>invisible text layer]
        CMP[(notes/compressed<br/>canonical PDF store)]
        IDX[04 local_index.py<br/>BGE-small + BM25 + RRF]
        SQL[(data/local_index.sqlite)]
        APP[FastAPI dashboard<br/>FSRS + tutor + exam]
        GEM[Gemini API<br/>reasoning only]
    end

    SN --> DB
    SN --> WDOC
    SSH --> DB
    SSH --> WDOC
    SYNC --> SSH
    SYNC --> REC
    REC --> EXP
    EXP --> STAGE
    STAGE --> REP
    REP --> OCR
    OCR --> CMP
    CMP --> IDX
    IDX --> SQL
    SQL --> APP
    APP --> GEM
    APP --> SQL
```

## Data flow

1. **Sync** — `00_orchestrator/sync_and_export_notes.sh` acquires
   `ai_agend_history/orchestrator.lock`, checks the 55 GB / 15 GiB guardrails,
   starts the VM only if it was not already running, uploads
   `sync_now.sh`+`click_sync.ps1` to the guest, clicks **Sync now** through the
   interactive session, and waits for `IsDirty=0` plus read-only WDoc
   quiescence.
2. **UUID tree reconcile** — `reconcile_tree.py` takes one fresh guest inventory
   through a `mode=ro`/`query_only` SQLite helper. It matches by UUID, then
   manifest/state path and unique SHA-256. A moved/renamed unchanged UUID updates
   paths in place; it does not render, OCR or re-embed. Conflicts, recycled
   notes and unmapped PDFs are preserved in history archives. This project is
   compressed-only: it does not create or populate `notes/original/`.
3. **Delta export** — `incremental_export.py` compares modification time, title,
   folder and payload signatures in `incremental_state.sqlite`, then renders
   only changed/new/missing notes. Raw WDoc and candidates live under external
   `$SN_EXPORT_WORK` and are removed when the owning run exits. Page artifacts are cached
   by a content hash covering page bytes, media dependencies and renderer
   version/configuration.
4. **Repair and normalise** — `pdf_template_recovery.py` fixes legacy exports
   whose page count is correct but whose embedded PDF paper is missing. It reads
   visible order only from `pageIdInfo.dat`, binds `custom PDF page N` to
   `media/<name>.pdf` page `N-1`, proves the CLI drew only a background fill, and
   fails closed on ambiguous bindings. `pdf_color_normalize.py` whitens proven
   dark canvases before final derivation validation; embedded source-PDF paper
   is not modified.
5. **OCR** — `03_rtx_compression_ocr/paddle_ocr_pdf_batch.py` skips pages with
   ≥50 clean selectable characters, renders sparse pages, tries 0°/90°/270°,
   maps boxes back to the upright page, inserts invisible text, preserves native
   text, and uses conservative raster-drift validation before lossless
   compression with pikepdf.
6. **Index** — `04_learning_recall_app/my_notes_graph/src/local_index.py`
   discovers every PDF, classifies it as `searchable`, `empty` or `unreadable`,
   chunks page-aware text, and stores BM25 terms plus local BGE-small vectors in
   `data/local_index.sqlite`. Retrieval fuses lexical and dense ranks with
   reciprocal-rank fusion (`k=60`); no note text is sent to an embedding API.
7. **Study** — `00_main.py` and `engine/routes.py` serve the dashboard. Gemini
   is used only for reasoning (tutor, exam grading, question factory), with a
   server-side answer key, `countTokens` below the budget, bounded jittered
   retries, and immediate stop on 401/403.

## Storage layout

```
02_extracted_notes_pdf/
  notes/compressed/     sole canonical standalone PDF store
  notes/original/       legacy-only; must not be populated in compressed-only mode
  notes/export_manifest.json
  ai_agend_history/     state DBs, reports, safe archives, page cache
../python_libs_gpu/site-packages/  shared CUDA/Paddle/Torch packages
../python_libs/site-packages/     shared CPU and application packages
../sam_notes/_run_tmp_<pid>/      ephemeral run staging, removed on exit
04_learning_recall_app/my_notes_graph/
  data/local_index.sqlite   BM25 + dense index
  data/recall.sqlite        FSRS cards, logs, quiz bank, games
  data/memory.db            legacy study history (preserved)
  data/index.json           legacy JSON index (preserved)
```

`notes/compressed/` contains verified real PDFs. Prior differing versions,
duplicate copies and unambiguous orphans are moved into ignored private history
archives before replacement or pruning.

Publication always replaces the link atomically and never follows it for an
in-place write.

## Process and lock model

| Lock | Scope |
|---|---|
| `ai_agend_history/orchestrator.lock` | one full sync/export/index run |
| `ai_agend_history/gpu_ocr.lock` | serialized PaddleOCR GPU work |
| `data/local_index.lock` | index rebuild |
| `01_vm_windows_sync/vm/qemu.pid` + command-line check | VM ownership |

A pre-existing VM is never stopped by the runner; only a VM the runner started
is shut down via ACPI. The runner never force-kills QEMU.

## Security boundaries

- Samsung Notes production databases are read-only (`mode=ro`,
  `PRAGMA query_only=ON`).
- No note content is uploaded for embeddings or OCR.
- Gemini receives selected snippets and question context only when the user
  explicitly invokes a reasoning feature.
- Secrets live outside the repository and are never logged.
- The dashboard binds `127.0.0.1` by default; `RECALL_HOST=0.0.0.0` is an
  explicit opt-in for LAN/phone access.

## Failure philosophy

Every stage prefers **preserving the previous artifact** over publishing a
partial result:

- an unsupported page fails the note, not the whole run;
- a failed OCR derivative leaves the placeholder intact;
- a failed verification keeps the original and reports nonzero;
- a disk-guard refusal is a failure, not a silent skip;
- empty and unreadable sources are reported explicitly instead of being called
  searchable.
