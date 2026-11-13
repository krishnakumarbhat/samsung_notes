# Recall and learning app

The local FastAPI application combines spaced repetition (FSRS), quizzes,
study planning and offline hybrid retrieval. Its source, tests and static
assets are tracked; the SQLite index, notes mirror and study history are
local-only.

## Install and start

From the repository root:

```bash
bash scripts/bootstrap.sh --app
# Optional semantic search (CPU BGE model and weights):
bash scripts/bootstrap.sh --app --semantic --models
bash 04_learning_recall_app/my_notes_graph/run_app.sh
```

The default server listens on the local network for phone access; set
`RECALL_HOST=127.0.0.1` for loopback-only use. Open `http://127.0.0.1:8787`.
Stop it with:

```bash
bash 04_learning_recall_app/my_notes_graph/run_app.sh --stop
```

The root `.env` is optional for local study features. Gemini-powered tutoring
requires `GEMINI_API_KEY` or `GOOGLE_API_KEY`; without a key, local study and
retrieval features remain available. Never put API keys in source or reports.

## Local corpus index

Restore the private corpus first, then rebuild the index entirely offline:

```bash
bash 04_learning_recall_app/my_notes_graph/rebuild_index_offline.sh
curl -fsS http://127.0.0.1:8787/api/health
```

Semantic dependencies and model weights are optional. If unavailable, the
indexer uses deterministic lexical hashing; it makes no remote embedding
requests. The existing user database should be backed up before moving or
rebuilding a machine. Unit tests are in `tests/`; run the complete documented
test matrix with `python3 scripts/run_tests.py` from the repository root.
