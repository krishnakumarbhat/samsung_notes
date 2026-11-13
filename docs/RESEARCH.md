# Research and engineering decisions

This document consolidates the research that shaped the pipeline. It is a map
from **decision → evidence → measured result**. Detailed raw reports remain
local because they can expose personal note metadata; aggregate public evidence
is in `00_orchestrator/STAGE3_PUBLIC_SUMMARY.json`.

## 1. Problem constraints

| Constraint | Consequence |
|---|---|
| Samsung Notes sync is Windows-only and checks OEM identity | A Samsung-spoofed QEMU VM is used; the guest is treated as read-only |
| RTX 3050 laptop has 4 GB VRAM | CPU embeddings; OCR is serialized and retries at smaller raster sizes |
| Workspace cap 55 GB, 15 GiB free floor | Every writer checks disk headroom; OCR can defer rather than fill the disk |
| Note PDFs are personal and large | Git stores code/metadata only; payloads move in a checksummed bundle |
| Samsung cloud quota was observed over by 2.58 GB | No automatic deletion/replacement; lossless savings measured first |
| Model names in `.env` are not guaranteed API access | Auth failures stop retries; transient errors get bounded backoff |

## 2. Hybrid retrieval

### Decision

Use local **BGE-small-en-v1.5** dense embeddings plus a formula-aware **BM25**
lexical index, fused by **reciprocal-rank fusion** (RRF, `k=60`). Keep the
deterministic lexical-hashing embedder as a zero-download fallback. Never call a
remote embedding API for corpus text.

### Evidence

- Lù, *BM25S: Orders of magnitude faster lexical search via eager sparse
  scoring*, 2024 — <https://arxiv.org/abs/2407.03618>. Eager sparse scoring
  avoids repeated corpus scans; the implementation here keeps the same idea
  (precomputed term rows in SQLite) but does not claim the paper's speedups.
- Cormack, Clarke, Buettcher, *Reciprocal Rank Fusion outperforms Condorcet and
  individual Rank Learning Methods*, SIGIR 2009 —
  <https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf>. RRF combines ranks
  instead of mixing incomparable cosine and BM25 scores.
- BGE model card — <https://huggingface.co/BAAI/bge-small-en-v1.5>; BGE/C-Pack
  background — <https://arxiv.org/abs/2309.07597>. A small CPU model avoids
  competing with PaddleOCR for the 4 GB GPU.

### Engineering rules

- Tokenization preserves `x_i`, `d/dx`, `->`, `<=`, `==`, `0.01` and other
  formula/code tokens. English stopword removal is **not** applied to equations.
- Every discovered PDF gets exactly one status: `searchable`, `empty` or
  `unreadable`. Processing coverage and searchable coverage are reported
  separately.
- Chunks are page-aware, 900 characters with 120 overlap. A short formula-only
  page is kept rather than misclassified as empty.
- The index is incremental by SHA-256; unchanged PDFs are skipped.
- `data/index.json`, Chroma and `memory.db` are preserved read-only for history;
  new retrieval lives in `data/local_index.sqlite`.

### Measured result

274/274 PDFs processed, 248 searchable, 26 genuinely empty, 0 unreadable,
10,541 chunks. See the privacy-safe aggregate [`STAGE3_PUBLIC_SUMMARY.json`](../00_orchestrator/STAGE3_PUBLIC_SUMMARY.json).

## 3. PDF fidelity and page deltas

### Decision

Hash immutable page artifacts by content and dependencies; render only changed
pages; publish atomically; never lossy-recompress S-Pen ink or formulas.

### Evidence

- pikepdf page assembly and resource cleanup —
  <https://pikepdf.readthedocs.io/en/latest/topics/pages.html> and
  <https://pikepdf.readthedocs.io/en/latest/api/main.html>. Flate/object streams
  are lossless. JPEG recompression is lossy, WebP is not a generally supported
  PDF image filter, and lossy JBIG2 can substitute symbols in mathematical
  notes.
- The cache key includes page bytes, referenced media hashes, renderer
  version/config and layout dependencies. Ordered page UUIDs are separate from
  content keys, so inserting a page does not invalidate unrelated pages merely
  because indices shifted.
- Unknown ordering or incomplete dependency mapping falls back to a full-note
  render. Conservative invalidation is preferred to stale reuse.
- Raw `.page` file count is **not** a visible page count: deleted/hidden records
  and embedded-PDF paper differ.

### Embedded-PDF recovery

Legacy exports can keep the correct page count while dropping the embedded PDF
paper. Re-extracting through the same CLI cannot fix it.

Proof obligations implemented in `02_extracted_notes_pdf/script/pdf_template_recovery.py`:

1. Visible order comes only from `pageIdInfo.dat`.
2. `custom PDF page N` binds to `media/<name>.pdf` page `N-1`.
3. With several embedded PDFs, only unique contiguous runs with matching aspect
   ratios are accepted; ambiguity fails.
4. The CLI page must contain exactly one full-page background fill of the
   recorded colour, no ink/text/image operators, and a rectangle covering the
   rendered MediaBox.
5. Ink over embedded paper fails closed because transparent overlay composition
   is not proved.

**Measured:** two notes repaired; 54/54 and 32/32 embedded pages matched the
source PDFs pixel-exactly at 0.3 scale. One 27-page note with 39 ink strokes over
embedded paper remains unsupported and keeps its original plus a placeholder.
The detailed pixel-comparison report is local-only; aggregate results are in
the privacy-safe public summary.

### Defect audit and repair

- Audit inspected 548 files / 8,552 pages. It found 214 black-canvas files
  (3,980 black pages) and 21 missing-base-PDF candidates.
- After repair: **0 black files / 0 black pages**; 78 blank pages were verified
  as genuinely empty, not defects.
- 49 relative symlinks were checked; 0 defects.
- 2 pages / 349 source strokes were repainted from verified ink data, with the
  original backed up and the ink SHA-256 saved.
- Aggregate evidence: [`STAGE3_PUBLIC_SUMMARY.json`](../00_orchestrator/STAGE3_PUBLIC_SUMMARY.json).

## 4. OCR

### Decision

PaddleOCR on the GPU only for pages that need it; insert invisible text beneath
the existing artwork; verify visually and structurally before publication.

### Rules

- Pages with **≥50 non-whitespace selectable characters** bypass OCR. The
  counter strips whitespace so spaced-out title pages are not mistaken for rich
  text.
- Sparse pages are rendered and OCR'd at 0°, then 90° and 270°; boxes are mapped
  back onto the upright page.
- OCR text is embedded as invisible text; the page image is not replaced.
- PyMuPDF writes a temporary PDF; pikepdf applies lossless stream/object-stream
  compression; page count and syntax are checked before atomic publication.
- GPU OOM retries at smaller raster sizes for the 4 GB mobile profile.
- Only newly exported or explicitly selected files are processed. The whole
  library is never passed for validation.

### Measured result

- Backfill: 272 PDFs / 9,080 pages; sparse pages fell from 399 to 263 after 133
  files, with 0 failures and no lost text.
- Three validation samples (math, AI/ML, repaired export) had **0 visually
  changed pages**, 0 PDF syntax errors, and text growth from 644 → 11,845
  characters on the sparse AI/ML sample.
- Lossless compression saved only 0.43–2.04% per sample (1.88% combined).
- Aggregate evidence: [`STAGE3_PUBLIC_SUMMARY.json`](../00_orchestrator/STAGE3_PUBLIC_SUMMARY.json).

## 5. Compression and the cloud quota

Observed quota snapshot:

| Measure | Bytes |
|---|---:|
| Reported usage | 7,945,041,403 |
| Quota | 5,368,709,120 |
| Over quota | 2,576,332,283 (≈32%) |

Lossless recompression of 273 files (5.424 GB → 5.299 GB) saved 125.6 MB
(**2.4%**), with 10 files recompressed, 262 not smaller, 1 structure mismatch.
That is far short of the 32% needed. Therefore:

- No lossy downsampling, JBIG2 substitution, cloud replacement or deletion is
  automated.
- The quota endpoint is read-only.
- Local WDoc bytes, exported PDF bytes and cloud quota bytes are different
  representations and are not treated as equal.

The detailed cloud quota snapshot is local-only; aggregate pipeline evidence
is in the privacy-safe [`STAGE3_PUBLIC_SUMMARY.json`](../00_orchestrator/STAGE3_PUBLIC_SUMMARY.json).

## 6. Adaptive learning

### FSRS v5

Pinned `fsrs==5.1.3`. Scheduling uses the FSRS-v5 decay/factor and retention
estimate. Reference:
<https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm>.

- A review is graded and scheduled once per completed attempt.
- Exam answer keys stay server-side until finish (practice mode excepted).
- Timer deadlines are server-validated.
- Thirty-day correctness is smoothed with a Beta(1,1) prior and an uncertainty
  estimate. One lucky answer is not shown as established mastery.
- MCQ distractor tags are hypotheses about mistakes, not proof of the learner's
  internal reasoning.
- The 20-slot question rotation is exactly 30% INTUITION, 35%
  CALCULATION_STEP, 20% ADVERSARIAL_TRAP, 15% SYSTEM_APPLICATION, with no
  adjacent duplicate type.

### Gemini usage

- Reasoning only: tutor, exam generation/grading, question factory.
- No Gemini call for corpus embeddings.
- Retry policy: bounded exponential backoff with full jitter on 429/5xx/timeout;
  **stop immediately on 401/403**.
- `countTokens` enforces a real prompt budget (<2,000; default 1,800).
- Provider failure is visible; generic fake quizzes are not substituted.
- Opening the app does not call Gemini and does not count as a study session.
- Question factory is opt-in and exits if the network probe fails.

Measured live evidence: primary exam HTTP 200 in 15.84 s, tutor HTTP 200 in
5.95 s, primary 503 → fallback `FALLBACK_OK` in 44.11 s. See
The public repository deliberately excludes live prompts and responses.

## 7. Export and sync correctness

- The guest SQLite inventory is read-only and includes full folder ancestry.
- The exporter compares modification time, size and content hash; it never
  re-extracts the full library for a routine check.
- A completed replacement must pass: PDF header, `%%EOF`, pikepdf syntax check,
  expected page count and render comparison. Only then is it published.
- Existing verified exports are preserved if a replacement fails.
- The sync automation clicks **Sync now** in the interactive session and waits
  for `IsDirty=0` and WDoc quiescence. It never accepts Terms/Privacy on the
  user's behalf.
- A pre-existing VM is left running; only a runner-owned VM is stopped via ACPI.

## 8. Known limitations

1. 26 PDFs have no selectable text; they are `empty`, not searchable.
2. 46 compressed sources lack derived-manifest rows; all 274 physical PDFs are
   indexed from originals or derivatives.
3. One embedded-PDF note with ink overlay remains unsupported.
4. Chrome aborts the PDF iframe's first Range request before its native viewer
   retries; an independent Range GET returns HTTP 206 and the drawer renders.
5. Lossless compression cannot close the observed cloud-quota gap.
6. `tools/sdocx-cli` is a prebuilt Linux x86-64 artifact; its source is not in
   this repository.
7. The 34 GB VM disk is not backed up by Git. Copy it or rebuild it.

## 9. Tested state

The reference run on 2026-11-13 executed four suites:

| Suite | Tests |
|---|---:|
| App (dashboard, FSRS, quiz bank, local index) | 162 |
| Pipeline (exporter, delta, repair, colour) | 53 |
| OCR (bridge, invisible text, compression) | 12 |
| Guest setup (sync quiescence) | 3 |
| **Total** | **230** |

The historical Stage 3 report recorded 92/92 in a narrower run; the current
runner covers the expanded suites. See
[`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md) for the report map.
