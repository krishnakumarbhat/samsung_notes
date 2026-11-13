"""Fully local, incremental, whole-corpus PDF indexing with page-aware hybrid search.

Purpose
    Replace the old report-driven indexer with a 100% offline pass over the
    exported note PDFs.  No Gemini or HTTP embedding call is ever made.

Inputs
    ``root``: the notes tree (``notes/`` with ``compressed/`` and ``original/``)
    or the ``compressed/`` tree directly.

Outputs
    A private SQLite index (``data/local_index.sqlite``) with per-source status,
    page-aware chunks, BM25 term rows and local dense vectors, plus query
    citations and folder coverage.

Statuses
    ``searchable`` - text extracted, at least one chunk stored.
    ``empty``      - PDF opened but has no selectable text (never counted searchable).
    ``unreadable`` - file missing (broken symlink), encrypted, or extraction failed.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import struct
import threading
import time
from collections import Counter
from pathlib import Path

from src.config import DATA, NOTES_DIR, PROJECT_ROOT

DB_FILE = DATA / "local_index.sqlite"

#: Hard ceiling for the SQLite index plus any local embedding cache we may load.
CACHE_BUDGET_BYTES = 300 * 1024 * 1024
#: Refuse to start a corpus pass below this much free space.
MIN_FREE_BYTES = 15 * 1024**3

CHUNK_SIZE = 900
CHUNK_OVERLAP = 120
MIN_CHUNK_CHARS = 1  # A short formula is valid retrieval evidence, not an empty page.
BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
LEXICAL_WORD_FEATURES = 256
LEXICAL_CHAR_FEATURES = 128

STATUS_SEARCHABLE = "searchable"
STATUS_EMPTY = "empty"
STATUS_UNREADABLE = "unreadable"

#: Code/math aware tokenizer: keeps x_i, d/dx, ->, <=, ==, 0.01 and symbols.
_TOKEN = re.compile(r"[\w]+(?:[./:^][\w]+)*|==|!=|<=|>=|->|=>|::|[^\w\s]", re.UNICODE)
_SKIP_NAMES = re.compile(r"^(~\$|\._)")  # Office/macOS temp artefacts only
_SEMANTIC_MODEL_NAMES = ("BAAI/bge-small-en-v1.5", "bge-small-en-v1.5")
_EMBEDDER = None
_EMBEDDER_ERROR: str | None = None
# Uvicorn serves sync routes from a thread pool.  Two first requests can race
# model construction, which makes sentence-transformers materialise weights on
# the meta device and permanently cache a load failure.  Serialize loading.
_EMBEDDER_LOCK = threading.Lock()
_VECTOR_CACHE: dict[str, tuple[object, list[int]]] = {}


class BudgetError(RuntimeError):
    """Raised when indexing would exceed the local storage budget."""


# --------------------------------------------------------------------------- #
# Text + token helpers
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> list[str]:
    """Return lowercase code/math aware tokens, preserving short symbols."""
    return [token.casefold() for token in _TOKEN.findall(text or "") if token.strip()]


def _sha256(path: Path) -> str:
    """Hash a PDF with bounded memory so large files stay cheap."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_page(text: str) -> str:
    """Normalise whitespace without dropping formula or code characters."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def chunk_page(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split one page's text into overlapping paragraph chunks, keeping its page."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        while len(paragraph) > size:
            if buffer:
                chunks.append(buffer.strip())
                buffer = ""
            chunks.append(paragraph[:size])
            paragraph = paragraph[size - overlap :]
        if buffer and len(buffer) + len(paragraph) > size:
            chunks.append(buffer.strip())
            buffer = buffer[-overlap:]
        buffer += ("\n\n" if buffer else "") + paragraph
    if buffer.strip():
        chunks.append(buffer.strip())
    kept = [chunk for chunk in chunks if len(chunk) > MIN_CHUNK_CHARS]
    if not kept and text.strip():
        kept = [text.strip()]  # keep formula-only/short pages rather than calling them empty
    return kept


# --------------------------------------------------------------------------- #
# Path resolution (compressed/ holds placeholders; original/ holds the bytes)
# --------------------------------------------------------------------------- #
def _roots(root: Path | None) -> tuple[Path, Path]:
    """Resolve the scanned PDF tree and the untouched ``original/`` fallback tree."""
    root = Path(root) if root is not None else Path(NOTES_DIR or "")
    if (root / "compressed").is_dir():
        return (root / "compressed").resolve(), (root / "original").resolve()
    if root.name == "compressed":
        return root.resolve(), (root.parent / "original").resolve()
    return root.resolve(), root.resolve()


def _candidate_targets(path: Path, pdf_root: Path, original_root: Path) -> list[Path]:
    """Build ordered real-file candidates for a possibly broken symlink."""
    candidates: list[Path] = []
    try:
        if path.is_file():
            candidates.append(path.resolve())
    except OSError:
        pass
    if path.is_symlink():
        try:
            raw = os.readlink(path)
        except OSError:
            raw = ""
        if raw:
            target = Path(raw)
            target = target if target.is_absolute() else (path.parent / target)
            candidates.append(target)
            # Export layout moved from .../notes/<folder> to .../notes/original/<folder>;
            # old links keep "./original" or "../original" segments that no longer resolve.
            resolved_parts = list(target.parts)
            for index, part in enumerate(resolved_parts):
                if part == "original":
                    candidates.append(Path(*resolved_parts[: index + 1], *resolved_parts[index + 2 :]))
                    break
    try:
        relative = path.relative_to(pdf_root)
    except ValueError:
        relative = Path(path.name)
    candidates.append(original_root / relative)
    return candidates


def resolve_pdf(path: Path, pdf_root: Path, original_root: Path) -> Path | None:
    """Return the readable real PDF for a placeholder path, or ``None``."""
    for candidate in _candidate_targets(path, pdf_root, original_root):
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _source_name(path: Path, pdf_root: Path, original_root: Path) -> str:
    """Prefer the compressed-tree relative path, falling back to the original tree."""
    for base in (pdf_root, original_root):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name


def _folder_of(source: str) -> str:
    """Top-level export folder used for coverage and scoped search."""
    parts = Path(source).parts
    return parts[0] if len(parts) > 1 else "Uncategorized"


# --------------------------------------------------------------------------- #
# Local embedding (explicit local model dir or cached model; lexical otherwise)
# --------------------------------------------------------------------------- #
DEFAULT_MODEL_DIR = PROJECT_ROOT / "05_environments_and_venvs" / "models" / "bge-small-en-v1.5"
_REQUIRED_FILES = ("config.json", "modules.json", "1_Pooling/config.json")
_TOKENIZER_FILES = ("tokenizer_config.json", "tokenizer.json", "vocab.txt")


def _model_dir() -> Path:
    """Resolve ``LOCAL_EMBED_MODEL`` (relative to the workspace) or the bundled default."""
    override = os.environ.get("LOCAL_EMBED_MODEL", "").strip()
    if not override:
        return DEFAULT_MODEL_DIR
    path = Path(override).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _cache_dir() -> Path:
    """Return the local fastembed/HuggingFace cache directory."""
    override = os.environ.get("LOCAL_INDEX_MODEL_CACHE") or os.environ.get("FASTEMBED_CACHE_PATH")
    return Path(override) if override else Path.home() / ".cache" / "fastembed"


def _cache_bytes(directory: Path) -> int:
    """Measure a cache/model directory without following symlinks outside it."""
    total = 0
    if not directory.exists():
        return 0
    for item in directory.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _model_status(path: Path | None = None) -> dict:
    """Validate a local bge-small directory without importing torch or downloading."""
    path = Path(path) if path is not None else _model_dir()
    missing = [name for name in _REQUIRED_FILES if not (path / name).is_file()]
    if not any((path / name).is_file() for name in _TOKENIZER_FILES):
        missing.append("tokenizer files")
    weights = [name for name in ("model.safetensors", "pytorch_model.bin", "onnx/model.onnx") if (path / name).is_file()]
    try:
        weight_bytes = sum((path / name).stat().st_size for name in weights) if weights else 0
    except OSError:
        weight_bytes = 0
    if not weights:
        missing.append("weights (model.safetensors/pytorch_model.bin/onnx)")
    elif weight_bytes < 1024 * 1024:
        missing.append("weights smaller than 1 MiB (partial download?)")
    size = _cache_bytes(path)
    complete = not missing
    if not path.exists():
        reason = f"local model directory absent: {path}"
    elif complete:
        reason = f"complete local model: {path} ({size // 1048576} MiB)"
    else:
        reason = f"incomplete local model at {path}: missing {', '.join(missing)}"
    return {
        "path": str(path),
        "exists": path.is_dir(),
        "complete": complete,
        "reason": reason,
        "missing": missing,
        "weights": weights,
        "mib": round(size / 1048576, 2),
    }


def _cached_snapshot() -> tuple[str, Path] | None:
    """Locate an already-downloaded fastembed/HF bge-small snapshot; never download."""
    cache = _cache_dir()
    if not cache.is_dir():
        return None
    for model in _SEMANTIC_MODEL_NAMES:
        for pattern in (f"models--{model.replace('/', '--')}", model.replace("/", "--")):
            base = cache / pattern
            if not base.is_dir():
                continue
            snapshots = base / "snapshots"
            if snapshots.is_dir():
                for snapshot in sorted(snapshots.iterdir()):
                    if (snapshot / "model.onnx").is_file() or (snapshot / "model.safetensors").is_file():
                        return model, snapshot
            if any(base.rglob("model.onnx")) or any(base.rglob("model.safetensors")):
                return model, base
    return None


def semantic_status() -> dict:
    """Report semantic availability from the explicit local dir or a model cache."""
    local = _model_status()
    result = {
        "available": False,
        "source": None,
        "path": local["path"],
        "model": None,
        "mib": local["mib"],
        "local": local,
        "reason": local["reason"],
    }
    if local["complete"]:
        result.update({
            "available": True,
            "source": "local-model-dir",
            "model": "BAAI/bge-small-en-v1.5",
            "reason": local["reason"],
        })
        return result
    cached = _cached_snapshot()
    if cached:
        name, snapshot = cached
        result.update({
            "available": True,
            "source": "model-cache",
            "path": str(snapshot),
            "model": name,
            "mib": round(_cache_bytes(snapshot) / 1048576, 2),
            "reason": f"cached model snapshot: {snapshot}",
        })
    return result


def _semantic_snapshot() -> tuple[str, str, Path, bool] | None:
    """Backward-compatible snapshot tuple: (label, model, path, explicit_dir)."""
    status = semantic_status()
    if not status["available"]:
        return None
    explicit = status["source"] == "local-model-dir"
    label = "local:bge-small-en-v1.5" if explicit else str(status["model"])
    return label, str(status["model"]), Path(status["path"]), explicit


class _LexicalEmbedder:
    """Deterministic offline dense fallback built from sklearn hashing features.

    This is a *lexical* projection (word tokens + character n-grams), not a
    semantic model.  It exists so retrieval still has a dense ranking signal
    with zero downloads while a real cached model is unavailable.
    """

    mode = "lexical-hashing"
    semantic = False
    model = "sklearn:HashingVectorizer(word 256 + char_wb 3-5 128)"

    def __init__(self) -> None:
        try:
            from sklearn.feature_extraction.text import HashingVectorizer
        except Exception as error:  # pragma: no cover - sklearn ships in the ocr env
            raise RuntimeError(f"sklearn unavailable: {type(error).__name__}") from error
        self._word = HashingVectorizer(
            n_features=LEXICAL_WORD_FEATURES,
            analyzer="word",
            tokenizer=tokenize,
            lowercase=False,
            norm="l2",
            alternate_sign=False,
            dtype="float32",
        )
        self._char = HashingVectorizer(
            n_features=LEXICAL_CHAR_FEATURES,
            analyzer="char_wb",
            ngram_range=(3, 5),
            norm="l2",
            alternate_sign=False,
            dtype="float32",
        )

    @property
    def dim(self) -> int:
        """Vector width of the concatenated word/character projection."""
        return LEXICAL_WORD_FEATURES + LEXICAL_CHAR_FEATURES

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode texts into a fixed-width, L2-normalised dense matrix."""
        if not texts:
            return []
        word = self._word.transform(texts)
        char = self._char.transform(texts)
        import numpy as np

        matrix = np.hstack([word.toarray(), char.toarray()]).astype("float32")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix /= norms
        return matrix.tolist()


class _SemanticEmbedder:
    """CPU-only local bge-small wrapper; never downloads, never uses CUDA."""

    semantic = True
    mode = "semantic"
    dim = 384

    def __init__(self, label: str, model: str, path: Path, explicit_dir: bool = True) -> None:
        self._label = label
        self.model = model if explicit_dir else str(path)
        self._snapshot = Path(path)
        self._explicit = explicit_dir
        self._backend = None
        self._backend_name = ""
        self._threads = max(1, int(os.environ.get("LOCAL_EMBED_THREADS", "2")))

    def _load(self):
        """Lazily open sentence-transformers (preferred) or fastembed, offline only."""
        if self._backend is not None:
            return self._backend
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("OMP_NUM_THREADS", str(self._threads))
        os.environ.setdefault("MKL_NUM_THREADS", str(self._threads))
        if not self._explicit:
            try:
                import inspect

                from fastembed import TextEmbedding

                if "local_files_only" in inspect.signature(TextEmbedding.__init__).parameters:
                    self._backend = TextEmbedding(
                        model_name=self._label,
                        cache_dir=str(_cache_dir()),
                        local_files_only=True,
                        threads=self._threads,
                    )
                    self._backend_name = "fastembed"
                    return self._backend
            except Exception:
                self._backend = None
        try:
            import torch

            torch.set_num_threads(self._threads)
            from sentence_transformers import SentenceTransformer

            self._backend = SentenceTransformer(
                str(self._snapshot), device="cpu", local_files_only=True,
            )
            self._backend_name = "sentence-transformers"
            return self._backend
        except Exception as error:
            self._backend = None
            raise RuntimeError(f"local semantic backend failed: {type(error).__name__}: {error}") from error

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode with the local CPU model; no network path exists."""
        backend = self._load()
        batch = max(1, int(os.environ.get("LOCAL_EMBED_BATCH", "16")))
        if self._backend_name == "fastembed":
            return [list(map(float, vector)) for vector in backend.embed(texts, batch_size=batch)]
        vectors = backend.encode(
            texts,
            batch_size=batch,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [list(map(float, vector)) for vector in vectors]


def get_embedder():
    """Return the active embedder, preferring a completed local semantic model.

    ``LOCAL_EMBED_MODEL`` points at a local directory (default
    ``05_environments_and_venvs/models/bge-small-en-v1.5``).  Semantic mode only
    activates when the directory is complete (or a snapshot is cached); this
    function never downloads anything and always has an explicit lexical
    fallback.  ``LOCAL_INDEX_EMBED_MODE=semantic`` fails loudly instead of
    silently degrading; ``lexical`` forces the deterministic fallback.
    """
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is not None:
            return _EMBEDDER
        _EMBEDDER = _build_embedder()
        return _EMBEDDER


def _build_embedder():
    """Construct the embedder while ``_EMBEDDER_LOCK`` is held."""
    global _EMBEDDER, _EMBEDDER_ERROR
    requested = os.environ.get("LOCAL_INDEX_EMBED_MODE", "auto").strip().lower()
    if requested in ("semantic", "auto"):
        status = semantic_status()
        if status["available"]:
            snapshot = _semantic_snapshot()
            if snapshot is not None and status["mib"] * 1048576 <= CACHE_BUDGET_BYTES:
                try:
                    candidate = _SemanticEmbedder(*snapshot)
                    candidate.encode(["probe"])
                    _EMBEDDER = candidate
                    return _EMBEDDER
                except Exception as error:  # bad/incomplete files: fall through or raise
                    _EMBEDDER_ERROR = f"local semantic backend failed: {type(error).__name__}: {error}"
                    if requested == "semantic":
                        raise RuntimeError(_EMBEDDER_ERROR) from error
            elif requested == "semantic":
                raise RuntimeError(f"semantic model exceeds {CACHE_BUDGET_BYTES // 1048576} MiB budget")
        elif requested == "semantic":
            raise RuntimeError(status["reason"])
    try:
        candidate = _LexicalEmbedder()
        candidate.encode(["probe"])  # validates numpy/scipy availability now, not mid-corpus
        _EMBEDDER = candidate
    except Exception as error:  # pragma: no cover - defensive only
        _EMBEDDER = _PureHashEmbedder()
        _EMBEDDER_ERROR = f"sklearn/numpy failed ({type(error).__name__}); using pure-python hashing"
    return _EMBEDDER


class _PureHashEmbedder:
    """Last-resort deterministic hashing embedder when sklearn is unavailable."""

    semantic = False
    mode = "lexical-pure-python"
    model = "sha256 bucket hashing"
    dim = 384

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Hash word and character n-grams into a normalised 384-d vector."""
        output = []
        for text in texts:
            vector = [0.0] * self.dim
            tokens = tokenize(text)
            features = tokens + [token[index : index + 3] for token in tokens for index in range(max(1, len(token) - 2))]
            for feature in features:
                bucket = int.from_bytes(hashlib.sha256(feature.encode("utf-8")).digest()[:4], "big") % self.dim
                vector[bucket] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            output.append([value / norm for value in vector])
        return output


def embedding_info() -> dict:
    """Describe embedding mode and semantic availability without any download."""
    status = semantic_status()
    embedder_error = _EMBEDDER_ERROR
    try:
        embedder = get_embedder()
    except Exception as error:
        embedder = None
        embedder_error = f"{type(error).__name__}: {error}"
    mode = getattr(embedder, "mode", "semantic-unavailable") if embedder else "semantic-unavailable"
    semantic = bool(getattr(embedder, "semantic", False))
    model_total = (
        round(_cache_bytes(_cache_dir()) / 1048576, 2)
        if status["source"] == "model-cache"
        else round(status["local"]["mib"] + _cache_bytes(_cache_dir()) / 1048576, 2)
    )
    if semantic:
        detail = f"semantic embeddings from {status['source']} ({status['path']})"
    elif embedder is not None and getattr(embedder, "mode", "").startswith("lexical"):
        if status["available"]:
            detail = (
                "semantic model is available but the process holds lexical embeddings; "
                "call reset_embedder() or restart to activate (currently NOT semantic)"
            )
        else:
            detail = (
                "no complete semantic model; deterministic lexical hashing (NOT semantic). "
                + status["reason"]
            )
    else:
        detail = status["reason"] if not embedder else "semantic unavailable"
    if embedder_error and not semantic:
        detail = f"{detail} | backend note: {embedder_error}"
    return {
        "mode": mode,
        "semantic": semantic,
        "lexical_not_semantic": not semantic,
        "model": getattr(embedder, "model", "") if embedder else "",
        "dim": int(getattr(embedder, "dim", 0) or 0) if embedder else 0,
        "network": "disabled",
        "external_api": False,
        "semantic_available": status["available"],
        "semantic_source": status["source"],
        "semantic_reason": status["reason"],
        "local_model_path": status["local"]["path"],
        "local_model_complete": status["local"]["complete"],
        "local_model_missing": status["local"]["missing"],
        "local_model_mib": status["local"]["mib"],
        "cached_semantic_available": status["available"],
        "cached_semantic_model": status["model"],
        "model_dir_env": os.environ.get("LOCAL_EMBED_MODEL", ""),
        "threads": max(1, int(os.environ.get("LOCAL_EMBED_THREADS", "2"))),
        "cache_dir": str(_cache_dir()),
        "cache_mib": round(_cache_bytes(_cache_dir()) / 1048576, 2),
        "cache_budget_mib": CACHE_BUDGET_BYTES // 1048576,
        "model_total_mib": round(model_total, 2),
        "detail": detail,
    }


def reset_embedder() -> None:
    """Clear the process-level embedder/vector caches (used after env changes)."""
    global _EMBEDDER, _EMBEDDER_ERROR
    with _EMBEDDER_LOCK:
        _EMBEDDER = None
        _EMBEDDER_ERROR = None
        _VECTOR_CACHE.clear()


def embed_signature(embedder=None) -> str:
    """Return the persisted embedding identity used to invalidate old vectors."""
    embedder = embedder or get_embedder()
    mode = getattr(embedder, "mode", "unknown")
    identity = f"chunk-v2-min1:{mode}:{int(getattr(embedder, 'dim', 0) or 0)}:{getattr(embedder, 'model', '')}"
    if getattr(embedder, "semantic", False):
        identity += f":{semantic_status()['path']}"
    return identity


# --------------------------------------------------------------------------- #
# SQLite schema
# --------------------------------------------------------------------------- #
def _db_path(db_path: Path | None = None) -> Path:
    """Resolve the index database location, defaulting to ``data/local_index.sqlite``."""
    return Path(db_path) if db_path is not None else Path(DB_FILE)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the index database with WAL-friendly, row-dict results."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    connection.execute(f"PRAGMA max_page_count={CACHE_BUDGET_BYTES // page_size}")
    _initialize(connection)
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    """Create the local-only schema if absent; existing data is never dropped."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL,
            folder TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            bytes INTEGER NOT NULL DEFAULT 0,
            pages INTEGER NOT NULL DEFAULT 0,
            chunks INTEGER NOT NULL DEFAULT 0,
            chars INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT,
            embed_mode TEXT,
            indexed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            page INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            n_tokens INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS terms (
            chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
            term TEXT NOT NULL,
            tf INTEGER NOT NULL,
            PRIMARY KEY (chunk_id, term)
        );
        CREATE TABLE IF NOT EXISTS vectors (
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            dim INTEGER NOT NULL,
            vec BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
        CREATE INDEX IF NOT EXISTS idx_terms_term ON terms(term);
        CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
        """
    )
    connection.commit()


def _pack(vector: list[float]) -> bytes:
    """Pack a dense vector as little-endian float32 for compact SQLite storage."""
    values = [float(value) for value in vector]
    return struct.pack(f"<{len(values)}f", *values)


def _unpack(blob: bytes) -> list[float]:
    """Unpack a float32 blob back into a list of floats."""
    if not blob:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


# --------------------------------------------------------------------------- #
# Budget checks
# --------------------------------------------------------------------------- #
def _check_budget(db_path: Path) -> dict:
    """Verify free space and cache/database size stay within the local budget."""
    probe = db_path.parent
    probe.mkdir(parents=True, exist_ok=True)
    try:
        usage = os.statvfs(str(probe))
        free = usage.f_bavail * usage.f_frsize
    except OSError:
        free = MIN_FREE_BYTES
    if free < MIN_FREE_BYTES:
        raise BudgetError(f"free space {free // 1048576} MiB below floor of {MIN_FREE_BYTES // 1048576} MiB")
    cache = _cache_bytes(_cache_dir()) + _cache_bytes(_model_dir())
    if cache > CACHE_BUDGET_BYTES:
        raise BudgetError(f"embedding cache/model {cache // 1048576} MiB exceeds {CACHE_BUDGET_BYTES // 1048576} MiB")
    database = db_path.stat().st_size if db_path.exists() else 0
    return {
        "free_mib": round(free / 1048576, 1),
        "min_free_mib": MIN_FREE_BYTES // 1048576,
        "cache_mib": round(cache / 1048576, 2),
        "cache_budget_mib": CACHE_BUDGET_BYTES // 1048576,
        "model_dir": str(_model_dir()),
        "db_mib": round(database / 1048576, 2),
        "db_budget_mib": CACHE_BUDGET_BYTES // 1048576,
    }


# --------------------------------------------------------------------------- #
# Source scanning, extraction and replacement
# --------------------------------------------------------------------------- #
def _scan(root: Path, pdf_root: Path, original_root: Path) -> list[tuple[Path, str, Path | None]]:
    """List PDF placeholders with their stable source names and resolved targets."""
    seen: dict[str, Path] = {}
    for path in sorted(pdf_root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(pdf_root).parts):
            continue  # atomic OCR workspaces are not corpus documents
        if not path.name.lower().endswith(".pdf") or _SKIP_NAMES.search(path.name):
            continue
        if not path.is_symlink() and not path.is_file():
            continue
        source = _source_name(path, pdf_root, original_root)
        seen.setdefault(source, path)
    return [(path, source, resolve_pdf(path, pdf_root, original_root)) for source, path in seen.items()]


def _extract_pages(pdf_path: Path) -> tuple[list[tuple[int, str]], int]:
    """Extract page-indexed text locally with PyMuPDF (no rasterisation, no writes)."""
    try:
        import fitz  # PyMuPDF
    except Exception as error:  # pragma: no cover - fitz ships in the ocr env
        raise RuntimeError(f"PyMuPDF unavailable: {type(error).__name__}") from error
    pages: list[tuple[int, str]] = []
    with fitz.open(str(pdf_path)) as document:
        total = document.page_count
        for index, page in enumerate(document):
            text = _clean_page(page.get_text("text"))
            if text:
                pages.append((index + 1, text))
    return pages, total


def _replace_source(
    connection: sqlite3.Connection,
    source: str,
    path: str,
    folder: str,
    digest: str,
    size: int,
    status: str,
    pages: int,
    chunks: list[tuple[int, str, list[float], int]],
    chars: int,
    embed_mode: str,
    error: str | None = None,
) -> None:
    """Atomically replace one source's rows so a crash cannot mix old/new chunks."""
    now = time.time()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT id FROM sources WHERE source=?", (source,)).fetchone()
        if row:
            source_id = int(row["id"])
            connection.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id=?)", (source_id,))
            connection.execute("DELETE FROM terms WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id=?)", (source_id,))
            connection.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            connection.execute(
                """UPDATE sources SET path=?,folder=?,sha256=?,bytes=?,pages=?,chunks=?,chars=?,
                status=?,error=?,embed_mode=?,indexed_at=? WHERE id=?""",
                (path, folder, digest, size, pages, len(chunks), chars, status, error, embed_mode, now, source_id),
            )
        else:
            cursor = connection.execute(
                """INSERT INTO sources
                (source,path,folder,sha256,bytes,pages,chunks,chars,status,error,embed_mode,indexed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source, path, folder, digest, size, pages, len(chunks), chars, status, error, embed_mode, now),
            )
            source_id = int(cursor.lastrowid)
        for ordinal, (page, text, vector, tokens) in enumerate(chunks):
            cursor = connection.execute(
                "INSERT INTO chunks(source_id,page,ordinal,text,n_tokens) VALUES(?,?,?,?,?)",
                (source_id, page, ordinal, text, tokens),
            )
            chunk_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO terms(chunk_id,term,tf) VALUES(?,?,?)",
                [(chunk_id, term, count) for term, count in Counter(tokenize(text)).items()],
            )
            if vector:
                connection.execute(
                    "INSERT INTO vectors(chunk_id,dim,vec) VALUES(?,?,?)",
                    (chunk_id, len(vector), _pack(vector)),
                )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('embed_mode',?)",
            (embed_mode,),
        )
        connection.execute("""INSERT INTO meta(key,value) VALUES('generation','1')
            ON CONFLICT(key) DO UPDATE SET value=CAST(meta.value AS INTEGER)+1""")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _prune(connection: sqlite3.Connection, present: set[str]) -> int:
    """Delete rows for sources that no longer exist anywhere in the corpus."""
    rows = connection.execute("SELECT id,source FROM sources").fetchall()
    removed = 0
    for row in rows:
        if row["source"] in present:
            continue
        source_id = int(row["id"])
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id=?)", (source_id,))
        connection.execute("DELETE FROM terms WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id=?)", (source_id,))
        connection.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM sources WHERE id=?", (source_id,))
        connection.execute("""INSERT INTO meta(key,value) VALUES('generation','1')
            ON CONFLICT(key) DO UPDATE SET value=CAST(meta.value AS INTEGER)+1""")
        connection.commit()
        removed += 1
    return removed


def _index_all(root: Path | None = None, db_path: Path | None = None, force: bool = False) -> dict:
    """Incrementally index every PDF under ``root``; never calls Gemini or HTTP.

    Unchanged SHA-256 digests are skipped (unless ``force``), changed sources
    are replaced row-by-row in one transaction, deleted PDFs are pruned, and
    non-PDF files are ignored.  The return value separates *processed*,
    *searchable*, *empty*, and *unreadable* counts so empty scans never look
    like successful searchable coverage.
    """
    db_path = _db_path(db_path)
    budget = _check_budget(db_path)
    pdf_root, original_root = _roots(root)
    if not pdf_root.is_dir():
        raise FileNotFoundError(f"notes PDF tree not found: {pdf_root}")
    started = time.time()
    embedder = get_embedder()
    embed_mode = embed_signature(embedder)
    files = _scan(pdf_root, pdf_root, original_root)
    connection = _connect(db_path)
    summary = {
        "root": str(pdf_root),
        "original_root": str(original_root),
        "db": str(db_path),
        "embedding": embedding_info(),
        "processed": 0,
        "indexed": 0,
        "unchanged": 0,
        "removed": 0,
        "searchable": 0,
        "empty": 0,
        "unreadable": 0,
        "chunks": 0,
        "budget": budget,
        "errors": [],
    }
    try:
        for _path, source, target in files:
            summary["processed"] += 1
            folder = _folder_of(source)
            if target is None:
                summary["unreadable"] += 1
                summary["errors"].append({"source": source, "error": "missing target PDF"})
                _replace_source(connection, source, str(pdf_root / source), folder, "", 0,
                                STATUS_UNREADABLE, 0, [], 0, embed_mode, "missing target PDF")
                continue
            try:
                digest = _sha256(target)
                size = target.stat().st_size
            except OSError as error:
                summary["unreadable"] += 1
                summary["errors"].append({"source": source, "error": type(error).__name__})
                _replace_source(connection, source, str(target), folder, "", 0,
                                STATUS_UNREADABLE, 0, [], 0, embed_mode, type(error).__name__)
                continue
            previous = connection.execute(
                """SELECT s.sha256,s.status,s.embed_mode,s.chunks,
                (SELECT COUNT(*) FROM vectors v JOIN chunks c ON c.id=v.chunk_id
                 WHERE c.source_id=s.id) AS vectors
                FROM sources s WHERE s.source=?""",
                (source,),
            ).fetchone()
            if (
                previous
                and not force
                and previous["sha256"] == digest
                and previous["embed_mode"] == embed_mode
                and previous["status"] in (STATUS_SEARCHABLE, STATUS_EMPTY)
                and (
                    previous["status"] == STATUS_EMPTY
                    or int(previous["vectors"]) >= int(previous["chunks"])
                )
            ):
                summary["unchanged"] += 1
                summary[previous["status"]] += 1
                summary["chunks"] += int(connection.execute(
                    "SELECT COUNT(*) AS n FROM chunks WHERE source_id=(SELECT id FROM sources WHERE source=?)",
                    (source,),
                ).fetchone()["n"])
                continue
            try:
                pages, total_pages = _extract_pages(target)
            except Exception as error:  # noqa: BLE001 - one bad PDF must not abort the corpus
                summary["unreadable"] += 1
                summary["errors"].append({"source": source, "error": f"{type(error).__name__}: {error}"[:240]})
                _replace_source(connection, source, str(target), folder, digest, size,
                                STATUS_UNREADABLE, 0, [], 0, embed_mode, type(error).__name__)
                continue
            page_chunks: list[tuple[int, str]] = []
            for page_number, page_text in pages:
                for chunk in chunk_page(page_text):
                    page_chunks.append((page_number, chunk))
            if not page_chunks:
                summary["empty"] += 1
                _replace_source(connection, source, str(target), folder, digest, size,
                                STATUS_EMPTY, total_pages, [], 0, embed_mode, None)
                continue
            # Reuse dense vectors for unchanged page chunks, including pure
            # insertions/reordering and a tokenizer/chunk-policy migration.
            old_vectors: dict[str, list[float]] = {}
            if previous and str(previous["embed_mode"]).removeprefix("chunk-v2-min1:") == embed_mode.removeprefix("chunk-v2-min1:"):
                old_vectors = {
                    row["text"]: _unpack(row["vec"])
                    for row in connection.execute(
                        """SELECT c.text,v.vec FROM chunks c JOIN vectors v ON v.chunk_id=c.id
                        JOIN sources s ON s.id=c.source_id WHERE s.source=?""", (source,))
                }
            missing_texts = list(dict.fromkeys(text for _page, text in page_chunks if text not in old_vectors))
            try:
                fresh_vectors = embedder.encode(missing_texts) if missing_texts else []
                old_vectors.update(zip(missing_texts, fresh_vectors))
                vectors = [old_vectors.get(text, []) for _page, text in page_chunks]
            except Exception as error:  # noqa: BLE001
                summary["errors"].append({"source": source, "error": f"embedding: {type(error).__name__}"})
                vectors = [[] for _ in page_chunks]
            prepared = [
                (page, text, vector, len(tokenize(text)))
                for (page, text), vector in zip(page_chunks, vectors)
            ]
            _replace_source(
                connection, source, str(target), folder, digest, size, STATUS_SEARCHABLE,
                total_pages, prepared, sum(len(text) for _page, text in page_chunks), embed_mode,
            )
            summary["indexed"] += 1
            summary["searchable"] += 1
            summary["chunks"] += len(prepared)
        summary["removed"] = _prune(connection, {source for _path, source, _target in files})
        summary["total_sources"] = int(connection.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"])
        summary["status_counts"] = {
            row["status"]: int(row["n"])
            for row in connection.execute("SELECT status, COUNT(*) AS n FROM sources GROUP BY status")
        }
        summary["indexed_processed"] = summary["processed"]
        summary["searchable_pct"] = round(100 * summary["searchable"] / summary["processed"]) if summary["processed"] else 0
        summary["elapsed_sec"] = round(time.time() - started, 2)
    finally:
        connection.close()
    return summary


def index_all(root: Path | None = None, db_path: Path | None = None, force: bool = False) -> dict:
    """Serialize corpus writers; readers retain consistent WAL snapshots meanwhile."""
    import fcntl
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _index_all(root=root, db_path=path, force=force)


# --------------------------------------------------------------------------- #
# Search: BM25 + dense RRF fusion with citations
# --------------------------------------------------------------------------- #
def _scope_clause(sources: list[str] | None, folder: str | None) -> tuple[str, list[object]]:
    """Build a SQL scope filter for explicit sources and/or a folder prefix."""
    clauses: list[str] = []
    params: list[object] = []
    if sources:
        clauses.append(f"s.source IN ({','.join('?' for _ in sources)})")
        params.extend(sources)
    if folder:
        clauses.append("(s.folder = ? OR s.source LIKE ?)")
        params.extend([folder.rstrip("/"), folder.rstrip("/") + "/%"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _bm25_scores(connection: sqlite3.Connection, query: str, clause: str, params: list[object]) -> dict[int, float]:
    """Score scoped chunks with the BM25 formula over persisted term rows."""
    terms = Counter(tokenize(query))
    if not terms:
        return {}
    placeholders = ",".join("?" for _ in terms)
    stats = connection.execute(
        f"""SELECT COUNT(*) AS n, AVG(c.n_tokens) AS avg_len
        FROM chunks c JOIN sources s ON s.id=c.source_id{clause}""",
        params,
    ).fetchone()
    total = int(stats["n"] or 0)
    average = float(stats["avg_len"] or 0.0) or 1.0
    if not total:
        return {}
    scope = f" AND {clause.strip()[6:]}" if clause else ""
    df_rows = connection.execute(
        f"""SELECT t.term, COUNT(*) AS df FROM terms t
        JOIN chunks c ON c.id=t.chunk_id JOIN sources s ON s.id=c.source_id
        WHERE t.term IN ({placeholders}){scope} GROUP BY t.term""",
        [*terms, *params],
    ).fetchall()
    document_frequency = {row["term"]: int(row["df"]) for row in df_rows}
    term_rows = connection.execute(
        f"""SELECT t.chunk_id, t.term, t.tf, c.n_tokens FROM terms t
        JOIN chunks c ON c.id=t.chunk_id JOIN sources s ON s.id=c.source_id
        WHERE t.term IN ({placeholders}){scope}""",
        [*terms, *params],
    ).fetchall()
    scores: dict[int, float] = {}
    for row in term_rows:
        term = row["term"]
        df = document_frequency.get(term, 1)
        inverse = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
        frequency = float(row["tf"])
        denominator = frequency + BM25_K1 * (1.0 - BM25_B + BM25_B * float(row["n_tokens"]) / average)
        scores[int(row["chunk_id"])] = scores.get(int(row["chunk_id"]), 0.0) + (
            terms[term] * inverse * frequency * (BM25_K1 + 1.0) / denominator
        )
    return scores


def _vector_cache(connection: sqlite3.Connection, clause: str, params: list[object]):
    """Load scoped vectors once per corpus shape; returns ids, matrix or None."""
    shape = connection.execute(
        f"SELECT COUNT(*) AS n, MAX(c.id) AS top FROM vectors v JOIN chunks c ON c.id=v.chunk_id "
        f"JOIN sources s ON s.id=c.source_id{clause}",
        params,
    ).fetchone()
    database = connection.execute("PRAGMA database_list").fetchone()[2]
    generation = connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
    key = f"{database}:{generation[0] if generation else 'legacy'}:{shape['n']}:{shape['top']}:{clause}:{params}"
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return cached
    rows = connection.execute(
        f"SELECT v.chunk_id, v.vec FROM vectors v JOIN chunks c ON c.id=v.chunk_id "
        f"JOIN sources s ON s.id=c.source_id{clause}",
        params,
    ).fetchall()
    if not rows:
        _VECTOR_CACHE.clear()
        _VECTOR_CACHE[key] = ([], [])
        return [], []
    try:
        import numpy as np

        matrix = np.frombuffer(b"".join(row["vec"] for row in rows), dtype="<f4")
        width = len(rows[0]["vec"]) // 4
        matrix = matrix.reshape(len(rows), width)
        ids = [int(row["chunk_id"]) for row in rows]
    except Exception:  # pragma: no cover - numpy ships with sklearn
        matrix = [_unpack(row["vec"]) for row in rows]
        ids = [int(row["chunk_id"]) for row in rows]
    _VECTOR_CACHE.clear()
    _VECTOR_CACHE[key] = (matrix, ids)
    return matrix, ids


def _dense_scores(query: str, matrix, ids: list[int]) -> dict[int, float]:
    """Cosine-rank scoped vectors against the local query embedding."""
    if not ids:
        return {}
    embedder = get_embedder()
    vector = embedder.encode([query])[0]
    if not vector:
        return {}
    try:
        import numpy as np

        scores = matrix @ np.asarray(vector, dtype="<f4")
        return {chunk_id: float(score) for chunk_id, score in zip(ids, scores)}
    except Exception:
        query_vector = [float(value) for value in vector]
        output = {}
        for chunk_id, stored in zip(ids, matrix):
            dot = sum(a * b for a, b in zip(stored, query_vector))
            output[chunk_id] = dot
        return output


def search(
    query: str,
    k: int = 3,
    max_chars: int = 3000,
    sources: list[str] | None = None,
    folder: str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return fused BM25+dense citations: ``source``, ``page``, ``text``, score.

    ``sources`` matches exact relative source paths; ``folder`` matches a
    top-level folder or a nested prefix (e.g. ``Mlaidlgenai_robo/Robotics``).
    The total returned text is capped by ``max_chars``.
    """
    if not (query or "").strip() or k <= 0:
        return []
    connection = _connect(_db_path(db_path))
    try:
        connection.execute("BEGIN")  # one WAL snapshot for scores, vectors and citations
        clause, params = _scope_clause(sources, folder)
        bm25 = _bm25_scores(connection, query, clause, params)
        matrix, ids = _vector_cache(connection, clause, params)
        dense = _dense_scores(query, matrix, ids) if ids else {}
        fusion: dict[int, float] = {}
        for rank, chunk_id in enumerate(
            sorted(bm25, key=lambda key: bm25[key], reverse=True)[: max(k * 8, 20)], start=1
        ):
            fusion[chunk_id] = fusion.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, chunk_id in enumerate(
            sorted(dense, key=lambda key: dense[key], reverse=True)[: max(k * 8, 20)], start=1
        ):
            fusion[chunk_id] = fusion.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        if not fusion:
            return []
        ordered = sorted(fusion, key=lambda key: (fusion[key], bm25.get(key, 0.0)), reverse=True)
        placeholders = ",".join("?" for _ in ordered)
        rows = connection.execute(
            f"""SELECT c.id,c.page,c.text,s.source,s.folder FROM chunks c
            JOIN sources s ON s.id=c.source_id WHERE c.id IN ({placeholders})""",
            ordered,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        budget = max(0, max_chars)
        output: list[dict] = []
        for chunk_id in ordered:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            text = str(row["text"])
            if budget <= 0:
                break
            snippet = text[:budget]
            output.append({
                "source": str(row["source"]),
                "page": int(row["page"]),
                "text": snippet,
                "folder": str(row["folder"]),
                "score": round(fusion[chunk_id], 6),
                "bm25": round(bm25.get(chunk_id, 0.0), 6),
                "dense": round(dense.get(chunk_id, 0.0), 6),
            })
            budget -= len(snippet)
            if len(output) >= k:
                break
        return output
    finally:
        connection.close()


def sources(db_path: Path | None = None, status: str | None = None) -> list[dict]:
    """List indexed sources with status, page count, chunk count and folder."""
    connection = _connect(_db_path(db_path))
    try:
        sql = "SELECT * FROM sources"
        params: list[object] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY source"
        return [
            {
                "source": row["source"],
                "path": row["path"],
                "folder": row["folder"],
                "status": row["status"],
                "pages": int(row["pages"]),
                "chunks": int(row["chunks"]),
                "chars": int(row["chars"]),
                "sha256": row["sha256"],
                "bytes": int(row["bytes"]),
                "embed_mode": row["embed_mode"],
                "error": row["error"],
                "indexed_at": row["indexed_at"],
            }
            for row in connection.execute(sql, params)
        ]
    finally:
        connection.close()


def coverage(root: Path | None = None, db_path: Path | None = None) -> dict:
    """Report corpus status per top-level folder, including the Robotics subsection."""
    pdf_root, original_root = _roots(root)
    connection = _connect(_db_path(db_path))
    try:
        rows = connection.execute("SELECT source,status,chunks,pages FROM sources").fetchall()
    finally:
        connection.close()
    folders: dict[str, dict] = {}
    for row in rows:
        folder = _folder_of(str(row["source"]))
        item = folders.setdefault(folder, {
            "folder": folder, "pdfs": 0, "indexed_processed": 0,
            STATUS_SEARCHABLE: 0, STATUS_EMPTY: 0, STATUS_UNREADABLE: 0, "chunks": 0,
        })
        item["pdfs"] += 1
        item["indexed_processed"] += 1
        status = str(row["status"])
        item[status] = item.get(status, 0) + 1
        item["chunks"] += int(row["chunks"])
    robotics_prefix = "Mlaidlgenai_robo/Robotics"
    robotics_rows = [row for row in rows if str(row["source"]).startswith(robotics_prefix + "/")]
    if robotics_rows:
        robotics = {
            "folder": "Robotics",
            "folder_path": robotics_prefix,
            "pdfs": len(robotics_rows),
            "indexed_processed": len(robotics_rows),
            STATUS_SEARCHABLE: sum(1 for row in robotics_rows if row["status"] == STATUS_SEARCHABLE),
            STATUS_EMPTY: sum(1 for row in robotics_rows if row["status"] == STATUS_EMPTY),
            STATUS_UNREADABLE: sum(1 for row in robotics_rows if row["status"] == STATUS_UNREADABLE),
            "chunks": sum(int(row["chunks"]) for row in robotics_rows),
            "subsection": True,
            "parent": "Mlaidlgenai_robo",
            "counted_in_total": False,
        }
        folders["Robotics"] = robotics
    for item in folders.values():
        item["searchable_pct"] = round(100 * item[STATUS_SEARCHABLE] / item["pdfs"]) if item["pdfs"] else 0
    totals = {
        "pdfs": sum(int(item["pdfs"]) for key, item in folders.items() if not item.get("subsection")),
        "indexed_processed": sum(int(item["indexed_processed"]) for key, item in folders.items() if not item.get("subsection")),
        STATUS_SEARCHABLE: sum(int(item[STATUS_SEARCHABLE]) for key, item in folders.items() if not item.get("subsection")),
        STATUS_EMPTY: sum(int(item[STATUS_EMPTY]) for key, item in folders.items() if not item.get("subsection")),
        STATUS_UNREADABLE: sum(int(item[STATUS_UNREADABLE]) for key, item in folders.items() if not item.get("subsection")),
        "chunks": sum(int(item["chunks"]) for key, item in folders.items() if not item.get("subsection")),
    }
    return {
        "root": str(original_root),
        "compressed_root": str(pdf_root),
        "embedding": embedding_info(),
        "budget": _check_budget(_db_path(db_path)),
        **totals,
        "searchable_pct": round(100 * totals[STATUS_SEARCHABLE] / totals["pdfs"]) if totals["pdfs"] else 0,
        "folders": sorted(folders.values(), key=lambda item: item["folder"].casefold()),
    }
