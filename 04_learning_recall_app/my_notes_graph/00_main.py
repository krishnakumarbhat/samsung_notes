"""Local-first FastAPI server for the Socratic recall dashboard.

Binds loopback by default and can be exposed on the LAN for phone sync; host
headers are restricted to loopback, mDNS names and private addresses.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse as StarletteJSONResponse
from pydantic import BaseModel, Field

from src import ingest, memory, review, store, tutor, local_index
from src.config import NOTES_DIR, ROOT, VAULT
from src.gemini import GeminiError
from engine import network

MAX_REQUEST_BYTES = 21 * 1024 * 1024
MAX_UPLOAD_FILE_BYTES = 15 * 1024 * 1024


class AskRequest(BaseModel):
    """Bounded question input; retrieved context is selected server-side."""

    question: str = Field(min_length=1, max_length=1200)


class ProbeRequest(BaseModel):
    """Optional folder/note scope for an adaptive Socratic question."""

    scope: str = Field(default="Daily Adaptive Mix", max_length=200)


class AnswerRequest(BaseModel):
    """A student's free-response answer to a previously generated probe."""

    probe_id: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=2000)


class RatingRequest(BaseModel):
    """The learner's final FSRS-v5 recall rating after seeing rubric feedback."""

    probe_id: int = Field(ge=1)
    grade: int = Field(ge=1, le=4)


class UploadFile(BaseModel):
    """One explicitly selected local file, base64-encoded by the browser."""

    name: str = Field(min_length=1, max_length=240)
    b64: str = Field(min_length=1, max_length=20_000_000)


class UploadRequest(BaseModel):
    """Bounded manual upload request; normal indexing uses local PDF paths."""

    files: list[UploadFile] = Field(min_length=1, max_length=20)


class IndexRequest(BaseModel):
    """Index only IDs from the last incremental export report."""

    limit: int = Field(default=20, ge=1, le=25)
    uuids: list[str] = Field(default_factory=list, max_length=25)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Migrate local memory, record today's study day, and warm Chroma locally."""
    memory.initialize()
    # Merely launching the server is not a study session. Actual learning actions
    # record activity so background restarts cannot erase inactivity evidence.
    if memory.get_syllabus():
        tutor.ensure_syllabus()  # enrich existing concepts locally; no startup API call
    # Local index is refreshed by the runner/explicit indexing, not by boot-time API calls.
    network.start_mdns()
    try:
        yield
    finally:
        network.stop_mdns()


app = FastAPI(
    title="Samsung Notes Socratic Recall",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
from src.learning_routes import router as learning_router
from engine.routes import router as flow_router
app.include_router(learning_router)
app.include_router(flow_router)


class LanHostGuard:
    """Reject public host names while allowing loopback and private LAN literals.

    This keeps DNS-rebinding pages from reading local notes through a browser
    while still letting a phone dial ``http://192.168.x.x:8787`` or
    ``recall.local``.  ``RECALL_ALLOWED_HOSTS`` adds explicit exceptions.
    """

    def __init__(self, application) -> None:
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"host", b"").decode("latin-1")
            hostname = raw.rsplit(":", 1)[0].strip("[]").lower()
            if not self._allowed(hostname):
                response = StarletteJSONResponse({"detail": "Invalid host header"}, status_code=400)
                await response(scope, receive, send)
                return
        await self.application(scope, receive, send)

    @staticmethod
    def _allowed(hostname: str) -> bool:
        if not hostname:
            return False
        if hostname in {"127.0.0.1", "localhost", "testserver", "::1"}:
            return True
        if hostname.endswith(".local"):
            return True
        extra = {entry.strip().lower()
                 for entry in os.environ.get("RECALL_ALLOWED_HOSTS", "").split(",")
                 if entry.strip()}
        if hostname in extra:
            return True
        try:
            return ipaddress.ip_address(hostname).is_private
        except ValueError:
            return False


app.add_middleware(LanHostGuard)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Reject oversized JSON uploads before reading them into memory."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            request_bytes = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid Content-Length"})
        if request_bytes > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"error": "request exceeds 21 MiB"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; img-src 'self' data:; frame-src 'self'; connect-src 'self'; "
        "worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """Serve the local single-page study UI."""
    return FileResponse(ROOT / "static" / "index.html", media_type="text/html")


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    """Serve the PWA manifest from the site root so the scope covers the app."""
    return FileResponse(ROOT / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    """Serve the service worker at the root: a nested path would limit its scope."""
    return FileResponse(ROOT / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> dict:
    """Simple local readiness check for the orchestrator."""
    return {"ok": True, "chunks": sum(row["chunks"] for row in local_index.sources()),
            "notes_dir": str(NOTES_DIR) if NOTES_DIR else None, "retrieval": "local-only"}


@app.get("/api/state")
def state() -> dict:
    """Return mastery, FSRS capacity, inactivity, coverage, and retrieval stats."""
    profile = memory.study_profile()
    due = review.due_topics(limit=profile["daily_cap"])
    return {
        "stats": memory.stats(),
        "profile": memory.learner_profile(),
        "qa_count": memory.qa_count(),
        "due": due,
        "chunks": sum(row["chunks"] for row in local_index.sources()),
        "study": profile,
        "coverage": ingest.coverage(),
        "mistakes": memory.mistake_patterns(limit=10),
    }


@app.get("/api/review")
def due_review() -> dict:
    """Return today's due FSRS queue under the inactivity governor's cap."""
    profile = memory.study_profile()
    return {"due": review.due_topics(limit=profile["daily_cap"]), "study": profile}


@app.get("/api/history")
def history() -> dict:
    """Return daily questions, answers, grades and mistake patterns."""
    return {"history": memory.history(), "mistakes": memory.mistake_patterns(limit=20)}


@app.get("/api/sources")
def sources() -> dict:
    """List indexed source paths and chunk counts."""
    return {"sources": local_index.sources(), "coverage": ingest.coverage()}


@app.get("/api/coverage")
def coverage() -> dict:
    """Return extracted, indexed, and mastered progress grouped by folder."""
    return ingest.coverage()


@app.get("/api/graph")
def graph() -> dict:
    """Return the concept prerequisite graph and topic mastery nodes."""
    return {"nodes": memory.stats(), "edges": memory.concept_edges()}


@app.get("/api/vault/{name:path}")
def vault_file(name: str) -> FileResponse:
    """Serve only files safely contained inside the local study vault."""
    target = (VAULT / name).resolve()
    if not target.is_relative_to(VAULT.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type = "image/svg+xml" if target.suffix.lower() == ".svg" else "text/markdown"
    return FileResponse(target, media_type=media_type)


@app.get("/source")
def source_file(path: str, page: int | None = None) -> FileResponse:
    """Serve an exported PDF page source, constrained to the note-export tree."""
    if not NOTES_DIR:
        raise HTTPException(status_code=404, detail="notes directory unavailable")
    candidate = Path(path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        # Logical citations use category-relative names and prefer searchable derived PDFs.
        if candidate.parts and candidate.parts[0] not in ("original", "compressed"):
            resolved = (NOTES_DIR / "compressed" / candidate).resolve()
        else:
            resolved = (NOTES_DIR / candidate).resolve()
        if not resolved.is_file() and "/" not in path and "\\" not in path:
            # Prefer the untouched original/ tree; compressed/ holds symlinks.
            candidates = [
                p for p in (NOTES_DIR / "original").rglob(path)
                if p.is_file() and not p.is_symlink()
            ]
            if len(candidates) == 1:
                resolved = candidates[0].resolve()
    if not resolved.is_relative_to(NOTES_DIR.resolve()) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="source not found")
    if resolved.suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="only PDF sources are served")
    headers = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(resolved.name)}"}
    if page is not None and page < 1:
        raise HTTPException(status_code=422, detail="page numbers start at 1")
    return FileResponse(resolved, media_type="application/pdf", headers=headers)


@app.post("/api/ask")
def ask(body: AskRequest) -> dict:
    """Answer using top-k hybrid retrieval and a hard prompt-token budget."""
    try:
        return tutor.ask(body.question)
    except GeminiError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/probe")
def probe(body: ProbeRequest) -> dict:
    """Generate one open-ended Socratic question from the selected scope."""
    # The new arena rejects provider failures rather than substituting generic quizzes.
    raise HTTPException(status_code=410, detail="Use /api/tutor/step or /api/exam/start with selected notes")


@app.post("/api/answer")
def answer(body: AnswerRequest) -> dict:
    """Rubric-grade an answer and persist the response before learner rating."""
    try:
        return tutor.evaluate_socratic_answer(body.probe_id, body.answer)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GeminiError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/rate")
def rate(body: RatingRequest) -> dict:
    """Commit the user's Again/Hard/Good/Easy FSRS rating."""
    try:
        return tutor.apply_fsrs_rating(body.probe_id, body.grade)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/grade")
def grade_compat(body: AnswerRequest) -> dict:
    """Compatibility route: evaluate and accept the Gemini-suggested grade."""
    try:
        return tutor.grade_socratic(body.probe_id, body.answer)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GeminiError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/index")
def index_updates(body: IndexRequest) -> dict:
    """Index only the incremental export report's updated PDFs."""
    try:
        return ingest.index_export_report(limit=body.limit, uuids=body.uuids or None)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"index update failed: {type(error).__name__}") from error


@app.post("/api/upload")
def upload(body: UploadRequest) -> dict:
    """Index explicitly selected small files; never forwards raw PDFs to Gemini."""
    results = []
    for file in body.files:
        try:
            raw = base64.b64decode(file.b64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=422, detail="invalid base64 upload") from error
        if len(raw) > MAX_UPLOAD_FILE_BYTES:
            raise HTTPException(status_code=413, detail="manual upload limit is 15 MiB per file")
        try:
            markdown, chunks = ingest.ingest_bytes(file.name, raw)
            tutor.upload(file.name, markdown[:1000], len(chunks))
            results.append({"file": file.name, "chunks": len(chunks)})
        except Exception as error:  # noqa: BLE001
            results.append({"file": file.name, "error": type(error).__name__})
    return {"results": results, "chunks": store.chunk_count()}
