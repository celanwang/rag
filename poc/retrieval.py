import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Literal

import httpx
import psycopg

from .documents import passage
from .models import Models
from .storage import Elasticsearch, KeywordError, Postgres, Scope

Mode = Literal["vector", "keyword", "rrf", "rerank"]
MODES = ("vector", "keyword", "rrf", "rerank")
BRANCHES = {
    "vector": ("vector",),
    "keyword": ("keyword",),
    "rrf": ("vector", "keyword"),
    "rerank": ("vector", "keyword"),
}
RRF_K = 60
logger = logging.getLogger(__name__)


class RetrievalUnavailable(Exception):
    pass


def rrf(rankings: list[list[str]]) -> list[str]:
    nonempty = [ranking for ranking in rankings if ranking]
    if len(nonempty) <= 1:
        return nonempty[0] if nonempty else []
    scores: dict[str, float] = defaultdict(float)
    for ranking in nonempty:
        for rank, chunk_id in enumerate(dict.fromkeys(ranking), 1):
            scores[chunk_id] += 1 / (RRF_K + rank)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))


class Retriever:
    def __init__(self, pg: Postgres, es: Elasticsearch, models: Models, executor: ThreadPoolExecutor):
        self.pg = pg
        self.es = es
        self.models = models
        self.executor = executor

    def recall(self, branch: str, scope: Scope, question: str, limit: int) -> tuple[list[str], dict]:
        started = perf_counter()
        if branch == "keyword":
            ids = self.es.search(scope, question, limit)
            return ids, {"keyword": (perf_counter() - started) * 1000}
        vector = self.models.query(question)
        embedded = perf_counter()
        ids = self.pg.search(scope, vector, limit)
        return ids, {
            "embedding": (embedded - started) * 1000,
            "vector": (perf_counter() - embedded) * 1000,
        }

    def retrieve(
        self, scope: Scope, question: str, mode: Mode,
        top_k: int, candidates: int, rerank_limit: int, debug: bool,
    ) -> dict:
        start = perf_counter()
        pending = {
            branch: self.executor.submit(self.recall, branch, scope, question, candidates)
            for branch in BRANCHES[mode]
        }
        recalled = {}
        stage_ms = {}
        degraded = []
        for branch, future in pending.items():
            try:
                recalled[branch], timings = future.result()
                stage_ms.update(timings)
            except (psycopg.Error, httpx.HTTPError, KeywordError) as error:
                logger.warning("Recall failed (%s): %s", branch, type(error).__name__)
                degraded.append(branch)
        if not recalled:
            raise RetrievalUnavailable("Recall unavailable")
        stage_ms["recall_wall"] = (perf_counter() - start) * 1000

        step = perf_counter()
        union = set().union(*recalled.values())
        authorized = self.pg.evidence(scope, union)
        rankings = {
            branch: [chunk_id for chunk_id in ids if chunk_id in authorized]
            for branch, ids in recalled.items()
        }
        stage_ms["authorization"] = (perf_counter() - step) * 1000

        step = perf_counter()
        fused = rrf(list(rankings.values()))
        stage_ms["fusion"] = (perf_counter() - step) * 1000

        ordered = fused
        if mode == "rerank" and fused:
            step = perf_counter()
            shortlist = fused[:rerank_limit]
            scores = self.models.rerank(question, [
                passage(authorized[chunk_id]["heading"], authorized[chunk_id]["body"])
                for chunk_id in shortlist
            ])
            ordered = [chunk_id for chunk_id, _ in sorted(
                zip(shortlist, scores, strict=True), key=lambda item: (-item[1], item[0])
            )]
            stage_ms["rerank"] = (perf_counter() - step) * 1000

        step = perf_counter()
        current = self.pg.evidence(scope, set(authorized))
        final = [current[chunk_id] for chunk_id in ordered if chunk_id in current][:top_k]
        stage_ms["authorization_final"] = (perf_counter() - step) * 1000
        result = {"results": final, "mode": mode, "degraded": degraded}
        if debug:
            result["debug"] = {
                "recall": {
                    branch: [chunk_id for chunk_id in ids if chunk_id in current]
                    for branch, ids in rankings.items()
                },
                "prefinal": [chunk_id for chunk_id in fused if chunk_id in current],
                "filtered": len(union) - len(current),
                "stage_ms": stage_ms,
                "service_ms": (perf_counter() - start) * 1000,
            }
        return result
