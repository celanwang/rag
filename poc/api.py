import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

import httpx
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .documents import fingerprint, passage, split_document
from .models import EMBEDDING, MODEL_THREADS, RERANKER, Models
from .retrieval import RRF_K, Mode, RetrievalUnavailable, Retriever
from .storage import Elasticsearch, KeywordError, Postgres, Scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    tenant: str
    subject: str
    libraries: tuple[str, ...] = ("demo",)
    can_ingest: bool = False

    def scope(self, library: str) -> Scope:
        if library not in self.libraries:
            raise HTTPException(403, "Knowledge base forbidden")
        return Scope(self.tenant, library, self.subject)


DEMO_IDENTITIES = {
    "alpha-admin": Identity("alpha", "admin", ("demo", "ops"), True),
    "alpha-reader": Identity("alpha", "reader"),
    "alpha-auditor": Identity("alpha", "auditor"),
    "beta-admin": Identity("beta", "admin", ("demo",), True),
    "beta-reader": Identity("beta", "reader"),
}


def identify(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))]):
    identity = DEMO_IDENTITIES.get(credentials.credentials) if credentials else None
    if identity is None:
        raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
    return identity


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=160)]
    content: str = Field(min_length=1, max_length=64_000)
    readers: list[Literal["admin", "reader", "auditor"]] = Field(default_factory=list, max_length=3)


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_base_id: str = "demo"
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    mode: Mode = "rerank"
    top_k: int = Field(default=5, ge=1, le=30)
    candidates: int = Field(default=50, ge=1, le=50)
    rerank_limit: int = Field(default=30, ge=1, le=30)
    debug: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    pg = Postgres()
    es = Elasticsearch()
    models = Models()
    with ThreadPoolExecutor(max_workers=8) as executor:
        try:
            pg.pool.open(wait=True, timeout=10)
            try:
                es.initialize()
            except (httpx.HTTPError, KeywordError):
                logger.warning("Keyword index unavailable at startup")
            app.state.pg = pg
            app.state.es = es
            app.state.models = models
            app.state.retriever = Retriever(pg, es, models, executor)
            yield
        finally:
            pg.pool.close()
            es.client.close()


app = FastAPI(title="RAG PoC", lifespan=lifespan)
Caller = Annotated[Identity, Depends(identify)]


@app.exception_handler(psycopg.Error)
@app.exception_handler(httpx.HTTPError)
@app.exception_handler(RetrievalUnavailable)
def unavailable(_request: Request, error: Exception):
    logger.warning("Retrieval or storage unavailable: %s", type(error).__name__)
    return JSONResponse({"detail": "Retrieval or storage unavailable"}, status_code=503)


@app.get("/health")
def health(request: Request):
    return {
        **request.app.state.pg.versions(), "elasticsearch": request.app.state.es.version(),
        "embedding": EMBEDDING, "reranker": RERANKER, "rrf_k": RRF_K, "model_threads": MODEL_THREADS,
    }


@app.post("/v1/knowledge-bases/{library}/documents")
def ingest(library: str, body: DocumentInput, identity: Caller, request: Request):
    scope = identity.scope(library)
    if not identity.can_ingest:
        raise HTTPException(403, "Document ingestion forbidden")
    if (
        not body.filename.lower().endswith((".md", ".txt"))
        or any(char in body.filename for char in ("/", "\\", "\x00"))
        or "\x00" in body.content
    ):
        raise HTTPException(422, "Expected a UTF-8 Markdown or text document")
    chunks = split_document(body.filename, body.content)
    if not chunks:
        raise HTTPException(422, "Document has no indexable text")
    state = request.app.state
    vectors = state.models.embed([passage(chunk.heading, chunk.body) for chunk in chunks])
    version = state.pg.publish(scope, body.filename, fingerprint(body.content), sorted(set(body.readers)), chunks, vectors)
    document_id = scope.document_id(body.filename)
    try:
        state.es.initialize()
        synced = state.pg.sync(scope, document_id, state.es)
        ready = synced == version
    except (httpx.HTTPError, KeywordError, psycopg.Error) as error:
        logger.warning("PG publish succeeded, keyword sync incomplete: %s", type(error).__name__)
        ready = False
    return {
        "document_id": document_id, "version_id": version, "chunk_count": len(chunks),
        "keyword_ready": ready,
    }


@app.post("/v1/retrieve")
def retrieve(body: SearchInput, identity: Caller, request: Request):
    scope = identity.scope(body.knowledge_base_id)
    if body.mode == "rerank" and body.top_k > body.rerank_limit:
        raise HTTPException(422, "top_k must be at most rerank_limit")
    return request.app.state.retriever.retrieve(
        scope, body.question, body.mode, body.top_k, body.candidates, body.rerank_limit, body.debug,
    )


@app.get("/v1/citations/{chunk_id}")
def citation(chunk_id: UUID, knowledge_base_id: str, identity: Caller, request: Request):
    scope = identity.scope(knowledge_base_id)
    evidence = request.app.state.pg.evidence(scope, {str(chunk_id)})
    if not evidence:
        raise HTTPException(404, "Citation unavailable")
    return evidence[str(chunk_id)]
