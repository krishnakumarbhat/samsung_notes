#!/usr/bin/env bash
# Opt-in background question pre-generator.
#
# This spends metered data: it only runs when you invoke it explicitly, and it
# exits immediately when the network probe fails.  The web app never starts it.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$APP_DIR/../.." && pwd -P)"
source "$PROJECT_ROOT/00_orchestrator/runtime.sh"

cd "$APP_DIR"
exec "$OPENCODE_PYTHON" -m engine.background_question_factory "$@"
