import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from .auth import InvalidKey, Library, LibraryForbidden, LibraryNotFound, app_session, require_library
from .db import SCHEMA_OPTIONS
from .documents import DocumentChange, InvalidReaders, change_document
from .evidence import CitationNotFound, Evidence, citation, original, original_is_current
from .ingest import DocumentNotFound, JobNotFound, JobStatus, RequestConflict, Submission, job_status, submit
from .keyword import KeywordIndex
from .models import BailianModels
from .retrieval import RetrievalUnavailable, Retriever, Search
from .text import MAX_UPLOAD_BYTES, InvalidText

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("RAG_DATABASE_URL")
    if not dsn:
        raise RuntimeError("Set RAG_DATABASE_URL before starting the API.")

    pool = ConnectionPool(
        dsn, min_size=1, max_size=8, timeout=3, open=False,
        kwargs={"autocommit": True, "options": f"{SCHEMA_OPTIONS} -c statement_timeout=3000"},
    )
    with ExitStack() as resources:
        resources.callback(pool.close)
        pool.open(wait=True, timeout=10)
        keyword = resources.enter_context(KeywordIndex.from_env())
        models = resources.enter_context(BailianModels.from_env()) if os.environ.get("DASHSCOPE_API_KEY") else None
        executor = resources.enter_context(ThreadPoolExecutor(max_workers=8))
        app.state.pool = pool
        app.state.retriever = Retriever(pool, keyword, models, executor)
        yield


app = FastAPI(title="RAG", lifespan=lifespan)


@app.exception_handler(psycopg.Error)
def database_error(_request: Request, error: psycopg.Error) -> JSONResponse:
    logger.error("Database operation failed (%s)", type(error).__name__)
    return JSONResponse({"detail": "Database unavailable"}, status_code=503)


@app.exception_handler(InvalidKey)
def invalid_key(_request: Request, _error: InvalidKey) -> JSONResponse:
    return JSONResponse({"detail": "Invalid API key"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(LibraryNotFound)
@app.exception_handler(DocumentNotFound)
@app.exception_handler(JobNotFound)
@app.exception_handler(CitationNotFound)
def not_found(_request: Request, error: Exception) -> JSONResponse:
    detail = {
        LibraryNotFound: "Knowledge base not found",
        DocumentNotFound: "Document not found",
        JobNotFound: "Ingestion job not found",
        CitationNotFound: "Citation not found",
    }[type(error)]
    return JSONResponse({"detail": detail}, status_code=404)


@app.exception_handler(LibraryForbidden)
def forbidden(_request: Request, error: LibraryForbidden) -> JSONResponse:
    return JSONResponse({"detail": f"Knowledge base {error} permission required"}, status_code=403)


@app.exception_handler(InvalidText)
def invalid_text(_request: Request, error: InvalidText) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=422)


@app.exception_handler(RequestConflict)
def upload_conflict(_request: Request, _error: RequestConflict) -> JSONResponse:
    return JSONResponse({"detail": "Idempotency key already used for a different upload"}, status_code=409)


@app.exception_handler(InvalidReaders)
def invalid_readers(_request: Request, _error: InvalidReaders) -> JSONResponse:
    return JSONResponse({"detail": "Readers must be applications in the same tenant"}, status_code=422)


@app.exception_handler(RetrievalUnavailable)
def unavailable(_request: Request, _error: RetrievalUnavailable) -> JSONResponse:
    return JSONResponse({"detail": "Retrieval unavailable"}, status_code=503)


def api_key(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    if credentials is None:
        raise InvalidKey
    return credentials.credentials


ApiKey = Annotated[str, Depends(api_key)]


@app.get("/v1/knowledge-bases/{code}")
def read_library(code: str, request: Request, key: ApiKey) -> Library:
    with app_session(request.app.state.pool, key) as (conn, identity):
        return require_library(conn, identity, code, "read")


@app.post("/v1/knowledge-bases/{code}/documents", status_code=202)
async def upload_document(
    code: str,
    request: Request,
    key: ApiKey,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[!-~]+$")],
    document_id: UUID | None = None,
) -> Submission:
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in {"", "text/markdown", "text/plain", "application/octet-stream"}:
        raise HTTPException(415, "Expected the original file as the request body")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File exceeds the 256 KiB upload limit")
        body.extend(chunk)
    return await run_in_threadpool(
        submit, request.app.state.pool, key, code, idempotency_key, filename, bytes(body), document_id,
    )


@app.get("/v1/ingestion-jobs/{job_id}")
def read_job(job_id: UUID, request: Request, key: ApiKey) -> JobStatus:
    return job_status(request.app.state.pool, key, job_id)


@app.post("/v1/retrieve")
def retrieve(search: Search, request: Request, key: ApiKey) -> dict:
    return request.app.state.retriever.retrieve(key, search)


@app.get("/v1/citations/{chunk_id}")
def read_citation(chunk_id: UUID, request: Request, key: ApiKey) -> Evidence:
    return citation(request.app.state.pool, key, chunk_id)


@app.get("/v1/documents/{document_id}/file")
def read_file(document_id: UUID, request: Request, key: ApiKey) -> Response:
    pool = request.app.state.pool
    source = original(pool, key, document_id)
    if source is None:
        raise DocumentNotFound
    body = source.content
    if len(body) > MAX_UPLOAD_BYTES or hashlib.sha256(body).digest() != source.checksum:
        logger.error("Original checksum mismatch document=%s version=%s", document_id, source.version_id)
        raise HTTPException(503, "Original unavailable")
    if not original_is_current(pool, key, document_id, source.version_id):
        raise DocumentNotFound
    return Response(body, media_type=source.mime, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(source.filename, safe='')}",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


class Readers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    readers: Annotated[list[UUID], Field(max_length=64)] | None


@app.patch("/v1/documents/{document_id}/readers")
def set_readers(document_id: UUID, change: Readers, request: Request, key: ApiKey) -> DocumentChange:
    return change_document(request.app.state.pool, key, document_id, readers=change.readers)


@app.delete("/v1/documents/{document_id}", status_code=202)
def delete_document(document_id: UUID, request: Request, key: ApiKey) -> DocumentChange:
    return change_document(request.app.state.pool, key, document_id, delete=True)
