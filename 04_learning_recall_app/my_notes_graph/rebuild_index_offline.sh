#!/usr/bin/env bash
# Rebuild the local BM25 + dense index after OCR or note changes.
#
# This path never calls Gemini and never downloads a model.  The semantic
# bge-small backend is used when its local dependencies import cleanly;
# otherwise the deterministic lexical-hashing embedder keeps every chunk
# densely indexed offline.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$APP_DIR/../.." && pwd -P)"
source "$PROJECT_ROOT/00_orchestrator/runtime.sh"

export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export LOCAL_INDEX_EMBED_MODE="${LOCAL_INDEX_EMBED_MODE:-auto}"
cd "$APP_DIR"

"$OPENCODE_PYTHON" - <<'PY'
"""Index the compressed corpus and print the machine-readable summary."""
import json

from src import local_index

summary = local_index.index_all()
print(json.dumps(summary, indent=2))
PY
