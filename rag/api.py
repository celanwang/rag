import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import ConnectionPool

from .auth import InvalidKey, Library, LibraryForbidden, LibraryNotFound, app_session, require_library

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("RAG_DATABASE_URL")
    if not dsn:
        raise RuntimeError("Set RAG_DATABASE_URL before starting the API.")

    pool = ConnectionPool(
        dsn, min_size=1, max_size=8, timeout=3, open=False,
        kwargs={"autocommit": True, "options": "-c statement_timeout=3000"},
    )
    try:
        pool.open(wait=True, timeout=10)
        app.state.pool = pool
        yield
    finally:
        pool.close()


app = FastAPI(title="RAG", lifespan=lifespan)


@app.exception_handler(psycopg.Error)
def database_error(_request: Request, error: psycopg.Error) -> JSONResponse:
    logger.error("Database operation failed (%s)", type(error).__name__)
    return JSONResponse({"detail": "Database unavailable"}, status_code=503)


@app.get("/v1/knowledge-bases/{code}")
def read_library(
    code: str,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Library:
    try:
        if credentials is None:
            raise InvalidKey
        with app_session(request.app.state.pool, credentials.credentials) as (conn, identity):
            return require_library(conn, identity, code, "read")
    except InvalidKey:
        raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"}) from None
    except LibraryNotFound:
        raise HTTPException(404, "Knowledge base not found") from None
    except LibraryForbidden:
        raise HTTPException(403, "Knowledge base read permission required") from None
