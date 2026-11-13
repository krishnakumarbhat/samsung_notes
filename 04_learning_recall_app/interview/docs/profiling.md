# Profiling Notes

Primary bottlenecks to inspect first:

- Resume parsing latency when loading large PDFs.
- ChromaDB query latency as interview history grows.
- Gemini round-trip latency when API-backed generation is enabled.
- Streamlit rerender cost when history tables become larger.

Suggested workflow:

```bash
env -u PYTHONPATH .venv/bin/python -m cProfile -o storage/profile.out -m streamlit run app.py --server.headless true
env -u PYTHONPATH .venv/bin/python - <<'PY'
import pstats
stats = pstats.Stats('storage/profile.out')
stats.sort_stats('cumtime').print_stats(25)
PY
```

Focus areas if latency becomes visible:

- Cache extracted resume text and derived focus areas.
- Batch semantic writes to ChromaDB if interview volume grows.
- Keep prompt context bounded to the top few relevant memories.
- Move external API calls behind explicit user actions only.
