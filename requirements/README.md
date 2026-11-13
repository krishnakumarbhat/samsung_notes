# Requirements map
#
#   app.txt       FastAPI dashboard, FSRS scheduling, PDF text extraction
#   semantic.txt  local BGE embeddings (CPU; ~2 GB)
#   export.txt    incremental exporter, page delta and PDF repair
#   ocr.txt       PaddleOCR + lossless compression libraries
#   dev.txt       pytest and repository tooling
#
# `bash scripts/bootstrap.sh --all` installs app + semantic + export + dev into
# a venv outside the repository.  OCR is opt-in because its CUDA wheels are
# large and machine-specific.
#
# These pins are the versions exercised by the 230-test local run recorded in
# docs/EVIDENCE_INDEX.md.  If you need a fully frozen tree, run:
#   python -m pip freeze > manifests/pip-freeze.txt
# on the machine that passes the tests.