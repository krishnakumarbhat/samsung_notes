# Samsung Notes export and PDF pipeline

This subsystem exports notes from the Windows guest into local-only PDF trees.
It is safe to clone without the guest, PDFs, OCR cache, or history database;
those assets are restored separately as described in the root
[`REPLICATION.md`](../REPLICATION.md).

## Layout

- `script/incremental_export.py` — read-only guest inventory and incremental
  export; preserves verified prior outputs on failure.
- `script/page_delta.py` — page-level cache and conservative invalidation.
- `script/pdf_template_recovery.py`, `repair_defective_notes.py` — verified,
  reversible PDF repair helpers.
- `notes/original/` and `notes/compressed/` — private, untracked data.
- `ai_agend_history/` — local state, caches and operational reports; not a
  source-control backup.

## Setup and use

1. Follow the [VM setup guide](../01_vm_windows_sync/README.md), synchronize
   Samsung Notes in the guest, and confirm the guest is reachable.
2. Set `SAMSUNG_NOTES_PASSWORD` in this directory's ignored `.env` file, or
   export it in the shell. Start from `.env.example`; never commit credentials.
3. Install exporter dependencies with `bash scripts/bootstrap.sh --export`.
4. Preview before writing, then run the incremental sync from the repository
   root:

   ```bash
   source 00_orchestrator/runtime.sh
   python 02_extracted_notes_pdf/script/incremental_export.py --dry-run
   bash 00_orchestrator/sync_and_export_notes.sh
   ```

The exporter requires a working Windows VM, `sshpass`, QEMU/USB or Wi-Fi guest
connectivity, and the bundled Linux x86-64 `tools/sdocx-cli`. It does not
modify the guest database. Keep transfer bundles encrypted: note PDFs and
operational state are private.

For OCR, see [`03_rtx_compression_ocr/README.md`](../03_rtx_compression_ocr/README.md).
For repairs, test on copies and retain the generated rollback data locally.
