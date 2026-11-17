import logging
from collections import defaultdict
from concurrent.futures import Executor
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

import httpx
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .evidence import ReadScope, load_evidence, read_scope, vector_candidates
from .keyword import KeywordIndex, KeywordIndexError
from .models import BailianModels, ModelError

Mode = Literal["vector", "keyword", "rrf", "rerank"]
Branch = Literal["vector", "keyword"]
BRANCHES: dict[Mode, tuple[Branch, ...]] = {
    "vector": ("vector",), "keyword": ("keyword",), "rrf": ("vector", "keyword"), "rerank": ("vector", "keyword"),
}
RRF_K = 60
logger = logging.getLogger(__name__)


class RetrievalUnavailable(Exception):
    pass


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_base_code: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    mode: Mode = "rrf"
    top_k: int = Field(default=5, ge=1, le=30)
    candidates: int = Field(default=50, ge=1, le=50)
    rerank_limit: int = Field(default=30, ge=1, le=30)
    debug: bool = False

    @model_validator(mode="after")
    def validate_rerank_limit(self) -> "Search":
        if self.mode == "rerank" and self.top_k > self.rerank_limit:
            raise ValueError("top_k must be at most rerank_limit for rerank mode")
        return self


def rrf(rankings: list[list[UUID]]) -> list[UUID]:
    nonempty = [ranking for ranking in rankings if ranking]
    if len(nonempty) <= 1:
        return nonempty[0] if nonempty else []
    scores: dict[UUID, float] = defaultdict(float)
    for ranking in nonempty:
        for rank, chunk_id in enumerate(dict.fromkeys(ranking), 1):
            scores[chunk_id] += 1 / (RRF_K + rank)
    return sorted(scores, key=lambda chunk: (-scores[chunk], chunk))


class Retriever:
    def __init__(
        self, pool: ConnectionPool, index: KeywordIndex, models: BailianModels | None, executor: Executor,
    ):
        self.pool = pool
        self.index = index
        self.models = models
        self.executor = executor

    def model_client(self) -> BailianModels:
        if self.models is None:
            raise ModelError("Bailian credentials are unavailable")
        return self.models

    def recall(self, branch: Branch, key: str, scope: ReadScope, search: Search) -> tuple[list[UUID], dict]:
        started = perf_counter()
        if branch == "keyword":
            ids = self.index.search(
                scope.identity.tenant, scope.library.id, scope.identity.app, search.question, search.candidates,
            )
            return ids, {"keyword": (perf_counter() - started) * 1000}

        embedding = self.model_client().query(search.question)
        embedded = perf_counter()
        ids = vector_candidates(self.pool, key, scope, embedding, search.candidates)
        return ids, {"embedding": (embedded - started) * 1000, "vector": (perf_counter() - embedded) * 1000}

    def retrieve(self, key: str, search: Search) -> dict:
        started = perf_counter()
        scope = read_scope(self.pool, key, search.knowledge_base_code)
        recall_started = perf_counter()
        pending = {
            branch: self.executor.submit(self.recall, branch, key, scope, search)
            for branch in BRANCHES[search.mode]
        }
        recalled = {}
        stage_ms = {}
        degraded = []
        for branch, future in pending.items():
            try:
                ids, timings = future.result()
                recalled[branch] = ids
                stage_ms.update(timings)
            except (httpx.HTTPError, KeywordIndexError, ModelError) as error:
                logger.warning("Recall unavailable branch=%s error=%s", branch, type(error).__name__)
                degraded.append(branch)
        if not recalled:
            raise RetrievalUnavailable
        stage_ms["recall_wall"] = (perf_counter() - recall_started) * 1000

        step = perf_counter()
        authorized = load_evidence(self.pool, key, scope, set().union(*recalled.values()))
        rankings = {
            branch: [chunk for chunk in dict.fromkeys(ids) if chunk in authorized]
            for branch, ids in recalled.items()
        }
        stage_ms["authorization"] = (perf_counter() - step) * 1000

        step = perf_counter()
        fused = rrf(list(rankings.values()))
        stage_ms["fusion"] = (perf_counter() - step) * 1000

        ordered = fused
        if search.mode == "rerank" and fused:
            step = perf_counter()
            shortlist = fused[:search.rerank_limit]
            try:
                scores = self.model_client().rerank(search.question, [authorized[chunk].passage() for chunk in shortlist])
                ordered = [chunk for chunk, _ in sorted(
                    zip(shortlist, scores, strict=True), key=lambda item: (-item[1], item[0]),
                )]
            except (httpx.HTTPError, ModelError) as error:
                logger.warning("Reranking unavailable error=%s", type(error).__name__)
                degraded.append("rerank")
            stage_ms["rerank"] = (perf_counter() - step) * 1000

        step = perf_counter()
        current = load_evidence(self.pool, key, scope, set(authorized))
        result = {
            "results": [current[chunk] for chunk in ordered if chunk in current][:search.top_k],
            "mode": search.mode,
            "degraded": degraded,
        }
        stage_ms["authorization_final"] = (perf_counter() - step) * 1000
        if search.debug:
            result["debug"] = {
                "recall": {branch: [chunk for chunk in ids if chunk in current] for branch, ids in rankings.items()},
                "prefinal": [chunk for chunk in fused if chunk in current],
                "stage_ms": stage_ms,
                "service_ms": (perf_counter() - started) * 1000,
            }
        return result
