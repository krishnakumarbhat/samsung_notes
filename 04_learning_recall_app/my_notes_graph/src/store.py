"""Hybrid BM25 + Chroma dense retrieval with prerequisite-term expansion."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from src import gemini
from src.config import CHROMA_DIR, DATA, NOTES_DIR

INDEX_FILE = DATA / "index.json"
CHUNK_SIZE = 1200
EMBED_BATCH_SIZE = 5  # keep each Gemini embedding request below the prompt budget
_TOKEN = re.compile(r"[\w]+(?:[./:][\w]+)*|==|!=|<=|>=|->|=>|[^\w\s]", re.UNICODE)
_embed_dead_until: datetime | None = None
_chroma_client = None
_chroma_collection = None
_chroma_error: str | None = None
_folder_by_basename: dict[str, str | None] | None = None


def tokenize(text: str) -> list[str]:
    """Preserve short code/math symbols instead of dropping them as stopwords."""
    return [token.casefold() for token in _TOKEN.findall(text) if token.strip()]


def document_id(source: str, text: str) -> str:
    """Create a stable ID suitable for Chroma upserts."""
    return hashlib.sha256(f"{source}\0{text}".encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    """Read the existing JSON fallback index without modifying it."""
    if not INDEX_FILE.exists():
        return []
    value = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"Recall index must be a JSON list: {INDEX_FILE}")
    return value


def _save(docs: list[dict]) -> None:
    """Atomically persist the sparse fallback and source metadata."""
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")
    temporary.replace(INDEX_FILE)


def _get_chroma_collection():
    """Lazily open the local persistent Chroma collection; never reset existing data."""
    global _chroma_client, _chroma_collection, _chroma_error
    if _chroma_collection is not None:
        return _chroma_collection
    if _chroma_error:
        return None
    try:
        import chromadb
        from chromadb.config import Settings

        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        _chroma_collection = _chroma_client.get_or_create_collection(
            name="samsung_note_chunks",
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )
        return _chroma_collection
    except Exception as error:  # keep local BM25 useful if Chroma is unavailable
        _chroma_error = type(error).__name__
        print(f"[store] Chroma unavailable ({_chroma_error}); using BM25 fallback")
        return None


def _collection_upsert(docs: list[dict]) -> int:
    """Upsert only precomputed dense vectors; sparse-only chunks remain in JSON."""
    collection = _get_chroma_collection()
    selected = [doc for doc in docs if isinstance(doc.get("vec"), list) and doc["vec"]]
    if not collection or not selected:
        return 0
    ids, embeddings, documents, metadatas = [], [], [], []
    for doc in selected:
        ids.append(doc.get("id") or document_id(doc["source"], doc["text"]))
        embeddings.append([float(value) for value in doc["vec"]])
        documents.append(doc["text"])
        metadata = {"source": str(doc["source"])}
        folder = _folder_for_source(str(doc["source"]), str(doc.get("folder", "")))
        if folder:
            metadata["folder"] = folder
        if doc.get("page") is not None:
            metadata["page"] = int(doc["page"])
        metadatas.append(metadata)
    collection.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    return len(ids)


def migrate_json_vectors_to_chroma() -> int:
    """Copy existing 3072-d JSON vectors into Chroma without API calls/re-embedding."""
    collection = _get_chroma_collection()
    if collection is None:
        return 0
    docs = _load()
    batches = [doc for doc in docs if isinstance(doc.get("vec"), list) and doc["vec"]]
    if not batches:
        return 0
    for start in range(0, len(batches), 128):
        _collection_upsert(batches[start : start + 128])
    return len(batches)


def _embed(texts: list[str]) -> list[list[float]] | None:
    """Embed small batches and apply a local circuit breaker after API failures."""
    global _embed_dead_until
    if _embed_dead_until and datetime.now() < _embed_dead_until:
        return None
    try:
        vectors = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            vectors.extend(gemini.embed(texts[start : start + EMBED_BATCH_SIZE]))
        return vectors
    except Exception as error:  # noqa: BLE001
        detail = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        print(
            f"[store] embeddings unavailable ({detail}); "
            f"keeping {len(vectors)} completed vectors and using BM25 for the rest"
        )
        _embed_dead_until = datetime.now() + timedelta(minutes=10)
        return vectors or None


def _keywords(text: str) -> dict[str, int]:
    """Return a sparse term-frequency map that retains code/equation symbols."""
    return dict(Counter(tokenize(text)))


def _page_from_chunk(text: str) -> int | None:
    match = re.search(r"(?m)^##\s+Page\s+(\d+)", text)
    return int(match.group(1)) if match else None


def _folder_for_source(source: str, explicit: str = "") -> str:
    """Resolve a legacy basename-only source to a unique folder when possible."""
    if explicit:
        return explicit
    source_path = Path(source)
    if len(source_path.parts) > 1:
        return source_path.parent.as_posix()
    global _folder_by_basename
    if _folder_by_basename is None:
        matches: dict[str, set[str]] = defaultdict(set)
        if NOTES_DIR and NOTES_DIR.is_dir():
            for pdf in NOTES_DIR.rglob("*.pdf"):
                matches[pdf.name].add(pdf.parent.relative_to(NOTES_DIR).as_posix())
        _folder_by_basename = {
            name: next(iter(folders)) if len(folders) == 1 else None
            for name, folders in matches.items()
        }
    return _folder_by_basename.get(source, "") or ""


def add(source: str, chunks: list[str], folder: str = "") -> int:
    """Upsert one file's chunks into BM25 JSON and dense Chroma storage."""
    docs = [doc for doc in _load() if doc.get("source") != source]
    vectors = _embed(chunks)
    new_docs: list[dict] = []
    for index, text in enumerate(chunks):
        doc: dict = {
            "id": document_id(source, text),
            "source": source,
            "folder": folder,
            "page": _page_from_chunk(text),
            "text": text,
            "kw": _keywords(text),
        }
        if vectors and index < len(vectors):
            doc["vec"] = [round(float(value), 7) for value in vectors[index]]
        new_docs.append(doc)
    docs.extend(new_docs)
    _save(docs)
    collection = _get_chroma_collection()
    if collection is not None:
        try:
            collection.delete(where={"source": source})
            _collection_upsert(new_docs)
        except Exception as error:  # retain JSON and sparse retrieval if Chroma fails
            print(f"[store] Chroma upsert failed ({type(error).__name__}); JSON index retained")
    return len(new_docs)


def sources() -> list[dict]:
    """Return indexed source names and chunk counts, largest first."""
    counts: dict[str, int] = {}
    for doc in _load():
        counts[doc["source"]] = counts.get(doc["source"], 0) + 1
    return [
        {"source": source, "folder": _folder_for_source(source), "chunks": count}
        for source, count in sorted(counts.items(), key=lambda item: -item[1])
    ]


def chunk_count() -> int:
    """Return locally indexed chunk count."""
    return len(_load())


def _bm25_rank(query: str, docs: list[dict], boost_terms: list[str] | None = None) -> list[tuple[str, float]]:
    """Compute BM25 scores over sparse terms, including exact math/code tokens."""
    if not docs:
        return []
    tokenized = [tokenize(doc.get("text", "")) for doc in docs]
    lengths = [len(tokens) for tokens in tokenized]
    average_length = sum(lengths) / max(1, len(lengths))
    document_frequency: Counter[str] = Counter()
    term_counts = []
    for tokens in tokenized:
        counts = Counter(tokens)
        term_counts.append(counts)
        document_frequency.update(counts.keys())
    query_terms = Counter(tokenize(query))
    for term in tokenize(" ".join(boost_terms or [])):
        query_terms[term] += 0.5
    count = len(docs)
    k1, b = 1.5, 0.75
    scored: list[tuple[str, float]] = []
    for index, counts in enumerate(term_counts):
        score = 0.0
        length = lengths[index]
        for term, query_weight in query_terms.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * length / max(1.0, average_length))
            score += query_weight * inverse * frequency * (k1 + 1.0) / denominator
        if score > 0:
            doc_id = docs[index].get("id") or document_id(docs[index]["source"], docs[index]["text"])
            scored.append((doc_id, score))
    return sorted(scored, key=lambda item: item[1], reverse=True)


def search(
    query: str,
    k: int = 3,
    max_chars: int = 3600,
    boost_terms: list[str] | None = None,
    source_filter: str | None = None,
    folder_filter: str | None = None,
) -> list[tuple[str, str]]:
    """Fuse BM25 and Chroma dense rankings, optionally scoped to a note/folder."""
    docs = _load()
    if source_filter:
        docs = [doc for doc in docs if str(doc.get("source", "")) == source_filter]
    if folder_filter:
        prefix = folder_filter.rstrip("/")
        docs = [
            doc for doc in docs
            if _folder_for_source(str(doc.get("source", "")), str(doc.get("folder", ""))).startswith(prefix)
        ]
    if not docs:
        return []
    by_id = {
        doc.get("id") or document_id(doc["source"], doc["text"]): doc
        for doc in docs
    }
    fusion: dict[str, float] = defaultdict(float)
    sparse = _bm25_rank(query, docs, boost_terms)
    for rank, (doc_id, _score) in enumerate(sparse[: max(k * 8, 20)], start=1):
        fusion[doc_id] += 1.0 / (60 + rank)

    query_vectors = _embed([query])
    collection = _get_chroma_collection() if query_vectors else None
    if collection is not None and collection.count() > 0:
        try:
            dense = collection.query(
                query_embeddings=[query_vectors[0]],
                n_results=collection.count() if (source_filter or folder_filter) else min(collection.count(), max(k * 8, 20)),
                include=["documents", "metadatas", "distances"],
            )
            dense_rank = 0
            for doc_id, metadata in zip(dense["ids"][0], dense["metadatas"][0]):
                metadata = metadata or {}
                source = str(metadata.get("source", ""))
                folder = str(metadata.get("folder", ""))
                if source_filter and source != source_filter:
                    continue
                if folder_filter and not _folder_for_source(source, folder).startswith(folder_filter.rstrip("/")):
                    continue
                dense_rank += 1
                rank = dense_rank
                fusion[doc_id] += 1.0 / (60 + rank)
        except Exception as error:  # noqa: BLE001
            print(f"[store] Chroma query failed ({type(error).__name__}); using BM25")

    ordered = sorted(fusion, key=lambda doc_id: fusion[doc_id], reverse=True)
    output: list[tuple[str, str]] = []
    budget = max_chars
    for doc_id in ordered:
        doc = by_id.get(doc_id)
        if not doc:
            continue
        text = str(doc.get("text", ""))
        snippet = text[:budget]
        source = str(doc.get("source", "unknown"))
        if snippet:
            output.append((source, snippet))
            budget -= len(snippet) + len(source) + 2
        if len(output) >= k or budget <= 0:
            break
    return output
