import logging
import re
from collections import defaultdict
from concurrent.futures import Executor
from datetime import date
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

import httpx
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .evidence import Evidence, ReadScope, load_evidence, neighbors, read_scope, vector_candidates
from .keyword import KeywordIndex, KeywordIndexError, identifiers
from .models import BailianModels, ModelError
from .policy import scope_hints

Mode = Literal["vector", "keyword", "rrf", "rerank"]
Branch = Literal["vector", "keyword"]
BRANCHES: dict[Mode, tuple[Branch, ...]] = {
    "vector": ("vector",), "keyword": ("keyword",), "rrf": ("vector", "keyword"), "rerank": ("vector", "keyword"),
}
# Keep early single-branch hits competitive with lower-ranked hits shared by both branches.
RRF_K = 1
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
    expand_neighbors: bool = False
    as_of: date | None = None
    interpret: bool = False

    @model_validator(mode="after")
    def validate_rerank_limit(self) -> "Search":
        if self.mode == "rerank" and self.top_k > self.rerank_limit:
            raise ValueError("top_k must be at most rerank_limit for rerank mode")
        return self


def rrf(rankings: list[list[UUID]], *, k: int = RRF_K) -> list[UUID]:
    nonempty = [ranking for ranking in rankings if ranking]
    if len(nonempty) <= 1:
        return nonempty[0] if nonempty else []
    scores: dict[UUID, float] = defaultdict(float)
    for ranking in nonempty:
        for rank, chunk_id in enumerate(dict.fromkeys(ranking), 1):
            scores[chunk_id] += 1 / (k + rank)
    return sorted(scores, key=lambda chunk: (-scores[chunk], chunk))


def order_policies(ids: list[UUID], evidence: dict[UUID, Evidence], search: Search) -> tuple[list[UUID], list[UUID]]:
    if search.as_of is None and not search.interpret:
        return ids, []
    policies = {chunk: policy for chunk in ids if (policy := evidence[chunk].policy()) is not None}
    topics = {policy.rule for policy in policies.values()
              if any(label and label in search.question for label in policy.topic_labels)}
    matching = {policy.rule for policy in policies.values() if policy.scope_label in search.question}
    eligible = {
        chunk: policy for chunk, policy in policies.items()
        if (search.as_of is None or policy.applies(search.as_of))
        and (not topics or policy.rule in topics)
        and (policy.rule not in matching or policy.scope_label in search.question)
    }
    groups = defaultdict(list)
    for chunk, policy in eligible.items():
        groups[policy.rule, policy.scope].append((chunk, policy))
    selected = set()
    conflict = set()
    for group in groups.values():
        present = {policy.id: policy for _, policy in group}
        superseded = {
            target for _, policy in group for target in policy.supersedes
            if target in present and policy.authority >= present[target].authority
            and policy.effective_from > present[target].effective_from
        }
        remaining = [(chunk, policy) for chunk, policy in group if policy.id not in superseded]
        if not remaining:
            continue
        authority = max(policy.authority for _, policy in remaining)
        winners = [(chunk, policy) for chunk, policy in remaining if policy.authority == authority]
        selected.update(chunk for chunk, _ in winners)
        if len({policy.value for _, policy in winners}) > 1:
            by_policy = {}
            for chunk, policy in winners:
                by_policy.setdefault(policy.id, chunk)
            conflict.update(by_policy.values())
    first = [chunk for chunk in ids if chunk in selected and policies[chunk].rule in matching | topics]
    ordered = first + [chunk for chunk in ids if chunk not in first and (chunk in selected or chunk not in policies)]
    return ordered, [chunk for chunk in ordered if chunk in conflict]


def identifier_hint(question: str, evidence: list[Evidence]) -> dict | None:
    visible: dict[str, list[str]] = defaultdict(list)
    for item in evidence:
        for code in identifiers(item.body):
            visible[code].append(item.heading or "")
    for requested in identifiers(question):
        if requested in visible:
            continue
        close = [code for code in visible if len(code) == len(requested) and
                 sum(left != right for left, right in zip(code, requested)) == 1]
        if not close:
            continue
        matches = [code for code in close if any(
            part in question for heading in visible[code] for part in re.findall(r"(?=([\u4e00-\u9fff]{3}))", heading)
        )]
        if len(matches) == 1:
            return {"identifier": requested, "suggested_identifier": matches[0]}
        if len(close) > 1:
            return {"identifier": requested, "possible_identifiers": sorted(close)}
    return None


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

        ordered, _ = order_policies(fused, authorized, search)
        shortlist = []
        if search.mode == "rerank" and ordered:
            step = perf_counter()
            shortlist = ordered[:search.rerank_limit]
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
        anchor_ids = ordered[:search.top_k]
        related = neighbors(self.pool, key, scope, anchor_ids) if search.expand_neighbors and anchor_ids else {}
        current = load_evidence(self.pool, key, scope, set(authorized) | set(related))
        ordered, _ = order_policies([chunk for chunk in ordered if chunk in current], current, search)
        result = {
            "results": [current[chunk] for chunk in ordered if chunk in current][:search.top_k],
            "mode": search.mode,
            "degraded": degraded,
        }
        if search.expand_neighbors:
            visible_anchors = {current[chunk].version_id for chunk in anchor_ids if chunk in current}
            primary = {evidence.chunk_id for evidence in result["results"]}
            supplements = []
            seen = {evidence.passage() for evidence in result["results"]}
            remaining = 640
            for chunk in (current[chunk] for chunk in related if chunk in current and chunk not in primary):
                if chunk.version_id not in visible_anchors or len(chunk.body) > remaining or chunk.passage() in seen:
                    continue
                if len(chunk.body.strip()) < 12 or chunk.body.rstrip().endswith(("：**", ":**", "：", ":")):
                    continue
                supplements.append(chunk)
                seen.add(chunk.passage())
                remaining -= len(chunk.body)
                if len(supplements) >= 4:
                    break
            result["supplemental"] = supplements
        if search.interpret or search.as_of is not None:
            eligible, conflicts = order_policies([chunk for chunk in fused if chunk in current], current, search)
            result["policy_conflicts"] = [current[chunk] for chunk in conflicts]
        if search.interpret:
            result["query_hints"] = {}
            hint = identifier_hint(search.question, [current[chunk] for chunk in eligible])
            if hint is not None:
                result["query_hints"].update(hint)
            policies = [policy for chunk in eligible if (policy := current[chunk].policy()) is not None]
            scopes = scope_hints(policies, search.question)
            if scopes:
                result["query_hints"]["possible_scopes"] = scopes
            result["needs_clarification"] = bool(scopes or (hint and "possible_identifiers" in hint))
        stage_ms["authorization_final"] = (perf_counter() - step) * 1000
        if search.debug:
            result["debug"] = {
                "recall": {branch: [chunk for chunk in ids if chunk in current] for branch, ids in rankings.items()},
                "prefinal": [chunk for chunk in fused if chunk in current],
                "rerank_input": [chunk for chunk in shortlist if chunk in current],
                "model_calls": {"embedding": int("embedding" in stage_ms), "rerank": int(bool(shortlist))},
                "stage_ms": stage_ms,
                "service_ms": (perf_counter() - started) * 1000,
            }
        return result
