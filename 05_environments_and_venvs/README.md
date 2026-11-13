# Environments and model assets

Do not copy the existing 9 GB OCR environment between operating systems. It
contains platform-specific native/CUDA libraries. The supported setup creates
the main Python environment outside the repository at
`~/.cache/sam_win/venv` and installs only requested components.

## Bootstrap recipes

```bash
# Recall app and shared utilities
bash scripts/bootstrap.sh --app

# Incremental PDF export / repair
bash scripts/bootstrap.sh --export

# OCR on CPU (large dependencies)
bash scripts/bootstrap.sh --ocr

# OCR with a compatible CUDA/Paddle GPU runtime
bash scripts/bootstrap.sh --ocr-gpu

# Optional local semantic retrieval and the pinned model
bash scripts/bootstrap.sh --app --semantic --models
```

Use Python 3.10–3.13. `requirements/` contains the component pins; semantic
retrieval and OCR are opt-in because of their download size and GPU/platform
constraints. If the semantic model is absent, retrieval falls back to local
lexical hashing. `models/bge-small-en-v1.5/` tracks only small configuration
and tokenizer files; model weights are downloaded from the pinned revision by
`scripts/fetch_models.py` and are never committed.

`python-envs/` contains only brief notes about an optional retired environment;
the machine-specific package inventory was excluded. Prefer the smaller
maintained requirement sets.
See [`REPLICATION.md`](../REPLICATION.md) for a fresh-OS checklist and
[`docs/TROUBLESHOOTING.md`](../docs/TROUBLESHOOTING.md) for native-library,
CUDA and pytest-plugin issues.
