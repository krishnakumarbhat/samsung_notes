#!/usr/bin/env bash
# Bootstrap a portable Python environment for the Samsung Notes workspace.
#
# The environment lives OUTSIDE the project by default.  Creating a venv on an
# external/NTFS mount is slow and breaks across remounts, so the default is
# ~/.cache/sam_win/venv.  Override with SAM_VENV=/path.
#
# Examples:
#   bash scripts/bootstrap.sh --app
#   bash scripts/bootstrap.sh --app --semantic --models
#   bash scripts/bootstrap.sh --all --ocr-gpu
#   bash scripts/bootstrap.sh --check-only
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${SAM_VENV:-$HOME/.cache/sam_win/venv}"
BASE_PYTHON="${SAM_PYTHON:-python3}"
COMPONENTS=()
DO_MODELS=0
DO_GPU=0
CHECK_ONLY=0
INIT_ENV=1
RUN_DOCTOR=1

usage() {
  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --app        FastAPI recall app, FSRS, PDF text extraction (default)
  --semantic   Local BGE embeddings (torch/transformers; ~2 GB)
  --export     pikepdf/PyMuPDF stack for the incremental exporter
  --ocr        PaddleOCR stack (CPU wheel by default)
  --ocr-gpu    Install paddlepaddle-gpu instead of the CPU wheel
  --dev        pytest, playwright and repository checks
  --all        app + semantic + export + dev (OCR is opt-in)
  --models     Download the pinned bge-small model (~130 MB)
  --venv PATH  Environment location (default: ~/.cache/sam_win/venv)
  --check-only Print the plan and exit
  --no-env     Do not copy .env.example to .env
  --no-doctor  Skip the final doctor run
  -h, --help   Show this help
EOF
}

while (($#)); do
  case "$1" in
    --app) COMPONENTS+=("app") ;;
    --semantic) COMPONENTS+=("semantic") ;;
    --export) COMPONENTS+=("export") ;;
    --ocr) COMPONENTS+=("ocr") ;;
    --ocr-gpu) COMPONENTS+=("ocr"); DO_GPU=1 ;;
    --dev) COMPONENTS+=("dev") ;;
    --all) COMPONENTS=("app" "semantic" "export" "dev") ;;
    --models) DO_MODELS=1 ;;
    --venv) VENV="$2"; shift ;;
    --check-only) CHECK_ONLY=1 ;;
    --no-env) INIT_ENV=0 ;;
    --no-doctor) RUN_DOCTOR=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if ((${#COMPONENTS[@]} == 0)); then
  COMPONENTS=("app")
fi

# Preserve order while removing duplicates.
UNIQUE=()
for component in "${COMPONENTS[@]}"; do
  found=0
  for existing in "${UNIQUE[@]:-}"; do
    [[ "$existing" == "$component" ]] && found=1 && break
  done
  ((found)) || UNIQUE+=("$component")
done
COMPONENTS=("${UNIQUE[@]}")

echo "Repository : $ROOT"
echo "Python     : $BASE_PYTHON"
echo "Venv       : $VENV"
echo "Components : ${COMPONENTS[*]}"
((DO_GPU)) && echo "Paddle     : GPU wheel"
((DO_MODELS)) && echo "Model      : download pinned bge-small-en-v1.5"
((CHECK_ONLY)) && { echo "Check-only: no changes made."; exit 0; }

if ! command -v "$BASE_PYTHON" >/dev/null 2>&1; then
  echo "Python 3.10+ is required (looked for '$BASE_PYTHON')." >&2
  exit 2
fi
"$BASE_PYTHON" - <<'PY'
import sys
if not ((3, 10) <= sys.version_info[:2] < (3, 14)):
    raise SystemExit(f"Python 3.10-3.13 required, found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} accepted")
PY

if [[ ! -x "$VENV/bin/python3" ]]; then
  echo "Creating venv at $VENV"
  "$BASE_PYTHON" -m venv "$VENV"
fi

PIP=("$VENV/bin/python3" -m pip)
"${PIP[@]}" install --upgrade pip wheel setuptools >/dev/null

install_requirements() {
  local file="$1"
  if [[ ! -f "$ROOT/requirements/$file" ]]; then
    echo "Missing requirements/$file" >&2
    exit 2
  fi
  echo "Installing requirements/$file"
  "${PIP[@]}" install -r "$ROOT/requirements/$file"
}

for component in "${COMPONENTS[@]}"; do
  case "$component" in
    app) install_requirements app.txt ;;
    semantic) install_requirements semantic.txt ;;
    export) install_requirements export.txt ;;
    ocr) install_requirements ocr.txt ;;
    dev) install_requirements dev.txt ;;
  esac
done

if [[ " ${COMPONENTS[*]} " == *" ocr "* ]]; then
  if ((DO_GPU)); then
    echo "Installing paddlepaddle-gpu==3.2.0 (CUDA runtime must already be present)"
    "${PIP[@]}" install "paddlepaddle-gpu==3.2.0"
  else
    echo "Installing paddlepaddle==3.2.0 (CPU)"
    "${PIP[@]}" install "paddlepaddle==3.2.0"
  fi
fi

mkdir -p \
  "$ROOT/02_extracted_notes_pdf/notes/original" \
  "$ROOT/02_extracted_notes_pdf/notes/compressed" \
  "$ROOT/02_extracted_notes_pdf/ai_agend_history" \
  "$ROOT/04_learning_recall_app/my_notes_graph/data" \
  "$ROOT/05_environments_and_venvs/models"

if ((INIT_ENV)); then
  for example in "$ROOT/.env.example" "$ROOT/02_extracted_notes_pdf/.env.example"; do
    target="${example%.example}"
    if [[ -f "$example" && ! -f "$target" ]]; then
      cp "$example" "$target"
      chmod 600 "$target" 2>/dev/null || true
      echo "Created ${target#$ROOT/} from its example (edit the secrets)."
    fi
  done
fi

# Make the prebuilt renderer available without copying it into ~/.local/bin.
if [[ -x "$ROOT/tools/sdocx-cli" ]]; then
  mkdir -p "$HOME/.local/bin"
  if [[ ! -e "$HOME/.local/bin/sdocx-cli" ]]; then
    ln -s "$ROOT/tools/sdocx-cli" "$HOME/.local/bin/sdocx-cli" 2>/dev/null || \
      cp "$ROOT/tools/sdocx-cli" "$HOME/.local/bin/sdocx-cli"
    echo "Linked sdocx-cli into ~/.local/bin"
  fi
fi

if ((DO_MODELS)); then
  "$VENV/bin/python3" "$ROOT/scripts/fetch_models.py"
fi

cat <<EOF

Environment ready: $VENV
Activate or export it:
  export SAM_PYTHON="$VENV/bin/python3"
  source 00_orchestrator/runtime.sh

Next:
  python scripts/doctor.py --mode all
  python scripts/run_tests.py
  bash 04_learning_recall_app/my_notes_graph/run_app.sh

The VM disk, exported PDFs and model weights are local-only. See REPLICATION.md.
EOF

if ((RUN_DOCTOR)); then
  SAM_PYTHON="$VENV/bin/python3" "$VENV/bin/python3" "$ROOT/scripts/doctor.py" --mode core || true
fi