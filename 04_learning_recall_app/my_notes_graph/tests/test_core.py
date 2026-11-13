"""Self-checks for chunking, keyword fallback search, and mastery math. Run: python -m tests.test_core"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# isolate storage in a temp dir BEFORE importing app modules
_TMP = Path(tempfile.mkdtemp(prefix="harness_test_"))
import src.config as cfg  # noqa: E402

cfg.DATA = _TMP
import src.memory as memory  # noqa: E402

memory.DB = _TMP / "test.db"
import src.review as review  # noqa: E402

review.DB = _TMP / "test.db"
import src.store as store  # noqa: E402

store.INDEX_FILE = _TMP / "test_index.json"

from src import notes  # noqa: E402


def test_chunk() -> None:
    """Chunks respect size cap and paragraph boundaries."""
    text = "\n\n".join("para " + "x " * 200 for _ in range(10))
    chunks = notes.chunk(text, size=500)
    assert all(len(c) <= 700 for c in chunks), max(len(c) for c in chunks)
    assert len(chunks) >= 3
    print("chunk ok:", len(chunks), "chunks")


def test_keyword_search(tmp: Path = None) -> None:
    """Fallback scoring ranks the matching chunk first without ollama."""
    docs = [
        {"source": "a", "text": "photosynthesis converts light energy into chemical energy in chloroplasts"},
        {"source": "b", "text": "backpropagation computes gradients of the loss with respect to weights"},
    ]
    kw = "gradient loss backpropagation weights"
    qw = store._keywords(kw)
    scored = [(sum(min(c, (store._keywords(d["text"])).get(w, 0)) for w, c in qw.items()), d["source"]) for d in docs]
    scored.sort(key=lambda s: -s[0])
    assert scored[0][1] == "b", scored
    print("keyword rank ok")


def test_mastery(tmp_db: Path = None) -> None:
    """record_answer + SM-2 accumulate; next_probe prefers due reviews, then untested."""
    memory.set_syllabus([{"name": "t1", "prereq": []}, {"name": "t2", "prereq": ["t1"]}])
    assert memory.next_probe_topic() == ("t1", False)
    memory.record_answer("t1", True)
    memory.record_answer("t1", True)
    memory.record_answer("t1", False)
    stats = {s["topic"]: s for s in memory.stats()}
    assert stats["t1"]["correct"] == 2 and stats["t1"]["total"] == 3
    assert memory.next_probe_topic() == ("t2", False)  # untested beats tested
    from src import review as srs
    review_result = srs.update("t2", 3)
    assert review_result["rating"] == 3
    assert review_result["next_review_days"] >= 0
    profile = memory.learner_profile()
    assert "weak" in profile or "not yet covered" in profile or "solid" in profile, profile
    print("mastery+srs ok |", profile)


if __name__ == "__main__":
    test_chunk()
    test_keyword_search()
    test_mastery()
    print("ALL PASS")
