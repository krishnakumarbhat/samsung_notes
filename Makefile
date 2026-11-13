# Samsung Notes workspace — portable entry points.
#
# `make help` lists every target.  Python defaults to $SAM_PYTHON, then the
# bootstrapped environment, then the current interpreter.

PY ?= $(or $(SAM_PYTHON),$(HOME)/.cache/sam_win/venv/bin/python3,python3)
SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

.PHONY: help setup app semantic export ocr ocr-gpu models doctor test test-fast verify manifest manifest-verify bundle run stop rebuild-index clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "; printf "Targets:\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Bootstrap the app environment (add ARGS="--semantic --export --dev")
	bash scripts/bootstrap.sh --app $(ARGS)

app: ## Install the FastAPI app dependencies only
	bash scripts/bootstrap.sh --app

semantic: ## Install local BGE embedding dependencies (~2 GB)
	bash scripts/bootstrap.sh --semantic

export: ## Install the incremental exporter dependencies
	bash scripts/bootstrap.sh --export

ocr: ## Install the PaddleOCR stack (CPU)
	bash scripts/bootstrap.sh --ocr

ocr-gpu: ## Install the PaddleOCR stack with the GPU wheel
	bash scripts/bootstrap.sh --ocr-gpu

models: ## Download the pinned bge-small-en-v1.5 weights
	$(PY) scripts/fetch_models.py

doctor: ## Check layout, dependencies, assets, disk and ports
	$(PY) scripts/doctor.py --mode all

test: ## Run every pytest suite with the correct environment
	$(PY) scripts/run_tests.py

test-fast: ## Run the fast app subset plus the pipeline suites
	$(PY) scripts/run_tests.py --fast

verify: ## Run the pre-push hygiene gate
	$(PY) scripts/verify_repo.py

manifest: ## Regenerate manifests/local_assets.json with SHA-256
	$(PY) scripts/assets_manifest.py generate --hash

manifest-verify: ## Verify local assets against the committed manifest
	$(PY) scripts/assets_manifest.py verify --hash

bundle: ## Pack notes/models/app data into 95 MB transfer parts (add ARGS="--include-vm")
	bash scripts/pack_local_assets.sh $(ARGS)

run: ## Start the recall dashboard on http://127.0.0.1:8787
	bash 04_learning_recall_app/my_notes_graph/run_app.sh

stop: ## Stop the recall dashboard gracefully
	bash 04_learning_recall_app/my_notes_graph/run_app.sh --stop

rebuild-index: ## Rebuild the local BM25 + dense index offline
	bash 04_learning_recall_app/my_notes_graph/rebuild_index_offline.sh

clean: ## Remove Python caches from the working tree
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete