# RTX OCR and lossless PDF processing

This directory contains the host-side PaddleOCR pipeline used for newly
exported or explicitly selected PDFs. It requires an explicit target so an
accidental command cannot process the complete Samsung Notes library.

## Page behavior

- Pages with at least 50 clean selectable characters bypass OCR.
- Sparse-text pages are rendered for PaddleOCR; accepted regions are embedded
  as invisible searchable text beneath the existing page artwork.
- PyMuPDF writes a temporary PDF; pikepdf then losslessly compresses streams
  and object streams. The output is checked for page-count and syntax changes
  before atomic publication. The source is not overwritten by this script.
- GPU mode uses Paddle's CUDA runtime when available; the 4 GB RTX 3050 profile
  uses mobile detection and retries smaller raster sizes on GPU OOM.

## Audit and backfill the whole corpus

```bash
# read-only page scan: writes ocr_coverage_report.json
python3 audit_pdf_text_coverage.py

# OCR only the sparse pages, verify, then replace each corpus PDF atomically
python3 backfill_searchable_text.py --device gpu --resume

# second pass: retry every still-sparse file, including sideways scans
python3 backfill_searchable_text.py --device gpu --refresh
```

`backfill_searchable_text.py` stages one PDF at a time under
`${TMPDIR:-/tmp}/sam_win_ocr` (override with `SAM_OCR_STAGING`), checks that the page count is unchanged and
that no text was lost, then swaps the file in. Progress is checkpointed in
`ocr_backfill_status.json`, so an interrupted run resumes where it stopped.
`--refresh` ignores the checkpoint for files the latest audit still lists as
sparse.

The OCR pass counts only non-whitespace characters when deciding whether a page
already has a text layer, so spaced-out title pages are not mistaken for rich
text. When a page yields little text, the pipeline also tries 90° and 270°
rotations and maps the boxes back onto the upright page before inserting the
invisible text.

Current corpus result: 272 PDFs / 9,080 pages; sparse pages fell from 399 to
263 after 133 files were processed, with 0 failures and no lost text.

## Run a selected target

```bash
python3 paddle_cuda_probe.py
python3 paddle_ocr_pdf_batch.py /path/to/one-note.pdf --device gpu --output-dir /tmp/ocr-test
```

For a deliberately selected directory, the relative folder hierarchy is
preserved in the output directory:

```bash
python3 paddle_ocr_pdf_batch.py /path/to/selected-pdfs --device gpu --output-dir /tmp/ocr-test
```

Do not pass the entire Samsung Notes library for validation. The incremental
runner invokes OCR only for newly exported PDFs, and the pipeline should be
proven on 2–3 copies before any wider batch.
