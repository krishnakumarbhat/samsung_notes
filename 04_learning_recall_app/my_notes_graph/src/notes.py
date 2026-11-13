"""Notes ingestion. Purpose: convert PDFs/markdown to text chunks for indexing. Inputs: file bytes/path. Outputs: list[str] chunks."""
import hashlib
import re
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader


def _pdf_to_md(reader: PdfReader) -> str:
    """Extract selectable PDF text page-by-page without rasterizing pages."""
    pages = []
    for i, page in enumerate(reader.pages):
        text = " ".join((page.extract_text() or "").split())
        if text:
            pages.append(f"## Page {i + 1}\n\n{text}")
    return "\n\n".join(pages)


def pdf_to_md(data: bytes) -> str:
    """Extract text from uploaded PDF bytes locally."""
    return _pdf_to_md(PdfReader(BytesIO(data), strict=False))


def pdf_path_to_md(path: Path) -> str:
    """Read a local PDF by path to avoid loading large files into memory."""
    return _pdf_to_md(PdfReader(str(path), strict=False))


def chunk(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    """Split text into overlapping paragraph-boundary chunks. Inputs: text, chunk size. Outputs: list of chunk strings."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        while len(p) > size:  # hard-split oversized paragraphs
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.append(p[:size])
            p = p[size - overlap :]
        if buf and len(buf) + len(p) > size:
            chunks.append(buf.strip())
            buf = buf[-overlap:]
        buf += ("\n\n" if buf else "") + p
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) > 40]


def ingest_bytes(name: str, data: bytes) -> tuple[str, list[str]]:
    """Convert an uploaded file to md + chunks. Inputs: filename, bytes. Outputs: (markdown_text, chunks)."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        md = pdf_to_md(data)
    elif suffix in (".md", ".txt"):
        md = data.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"unsupported file type: {suffix}")
    return md, chunk(md)


def ingest_path(path: Path) -> tuple[str, list[str]]:
    """Extract PDF/markdown text from disk and return bounded text chunks."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        markdown = pdf_path_to_md(path)
    elif suffix in (".md", ".txt"):
        markdown = path.read_text(encoding="utf-8", errors="replace")
    else:
        raise ValueError(f"unsupported file type: {suffix}")
    return markdown, chunk(markdown)


def file_digest(path: Path) -> str:
    """Hash a file using bounded-memory reads for incremental change detection."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
