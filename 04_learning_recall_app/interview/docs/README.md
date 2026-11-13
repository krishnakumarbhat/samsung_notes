# Interview Memory Coach

Local-first interview preparation tool for AI/ML and software engineering interviews.

Core capabilities:

- Default resume-driven interview flow using the local PDF in the workspace.
- Five interview sections: Resume, GitHub, Deep Dive, Coding, and HR.
- Persistent local memory with SQLite for history and ChromaDB for semantic recall.
- Spaced repetition so weak answers come back sooner and strong answers are deferred.
- Optional Gemini-backed question generation and answer evaluation.
- Temporary JD targeting through the UI without persisting the JD by default.

Run locally:

```bash
python3 -m venv .venv
env -u PYTHONPATH .venv/bin/pip install -e .[dev]
./scripts/run_app.sh
```

Environment variables:

- `GEMINI_API_KEY`: enables Gemini question generation and grading.
- `GEMINI_MODEL`: defaults to `gemini-3.1-flash-lite-preview`.
- `ENABLE_SUPABASE_SYNC`: remains `false` by default.
