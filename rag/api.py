import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import ConnectionPool

from .auth import InvalidKey, Library, LibraryForbidden, LibraryNotFound, app_session, require_library
from .db import SCHEMA_OPTIONS
from .ingest import DocumentNotFound, JobNotFound, JobStatus, RequestConflict, Submission, job_status, submit
from .objects import ObjectStore, StorageError
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
    objects = ObjectStore.from_env() if os.environ.get("RAG_S3_ENDPOINT") else None
    try:
        pool.open(wait=True, timeout=10)
        app.state.pool = pool
        app.state.objects = objects
        yield
    finally:
        pool.close()
        if objects:
            objects.close()


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
def not_found(_request: Request, error: Exception) -> JSONResponse:
    detail = {
        LibraryNotFound: "Knowledge base not found",
        DocumentNotFound: "Document not found",
        JobNotFound: "Ingestion job not found",
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


@app.exception_handler(StorageError)
def storage_error(_request: Request, _error: StorageError) -> JSONResponse:
    return JSONResponse({"detail": "Object storage unavailable"}, status_code=503)


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
    objects = request.app.state.objects
    if objects is None:
        raise StorageError(retryable=False)
    return await run_in_threadpool(
        submit, request.app.state.pool, objects, key, code, idempotency_key, filename, bytes(body), document_id,
    )


@app.get("/v1/ingestion-jobs/{job_id}")
def read_job(job_id: UUID, request: Request, key: ApiKey) -> JobStatus:
    return job_status(request.app.state.pool, key, job_id)
