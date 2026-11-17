import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from poc.benchmark import aggregate, gold_ids, metrics

from .models import EMBEDDING_MODEL, RERANK_MODEL
from .retrieval import BRANCHES, RRF_K
from .text import CHUNK_CHARS, CHUNK_OVERLAP, split_text

QUESTIONS = Path(__file__).resolve().parents[1] / "poc" / "questions.json"
BAD_CASES = Path(__file__).with_name("benchmark_cases")
MODES = tuple(BRANCHES)
TUNING = {"knowledge_base_code": "demo", "top_k": 5, "candidates": 8, "rerank_limit": 12, "debug": True}
CaseText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EvidenceAnchors = Annotated[list[tuple[CaseText, CaseText]], Field(min_length=1)]


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: CaseText
    token: Literal["alpha-reader", "alpha-auditor", "beta-reader"]
    kind: CaseText
    question: Annotated[CaseText, StringConstraints(max_length=240)]
    gold: EvidenceAnchors
    avoid_top1: EvidenceAnchors | None = None
    expected_behavior: CaseText | None = None
    variant_of: CaseText | None = None


CASE_LIST = TypeAdapter(Annotated[list[BenchmarkCase], Field(min_length=1)])


def load_cases(raw: bytes, *, require_behavior: bool = False) -> list[dict]:
    cases = CASE_LIST.validate_json(raw)
    by_id = {case.id: case for case in cases}
    if len(by_id) != len(cases):
        raise ValueError("Benchmark case IDs must be unique")
    for case in cases:
        if require_behavior and case.expected_behavior is None:
            raise ValueError(f"Missing expected behavior for {case.id}")
        if case.variant_of is not None:
            parent = by_id.get(case.variant_of)
            if parent is None or parent.variant_of is not None:
                raise ValueError(f"Variant {case.id} must reference a base case in the same suite")
    return [case.model_dump(mode="json", exclude_none=True) for case in cases]


def evidence_stages(gold: set[str], recall: dict[str, list[str]], prefinal: list[str], top_k: int) -> dict:
    candidates = {chunk for ids in recall.values() for chunk in ids}
    recalled_gold = gold & candidates
    prefinal_gold = gold.intersection(prefinal[:top_k])
    return {
        "candidate_recall": len(recalled_gold) / len(gold),
        "prefinal_recall_at_k": len(prefinal_gold) / len(gold),
        "missing_from_candidates": sorted(gold - candidates),
        "outside_prefinal_top_k": sorted(recalled_gold - prefinal_gold),
    }


def cli(*args: str) -> dict:
    return json.loads(subprocess.check_output([sys.executable, "-m", "rag.cli", *args], text=True))


def request(client: httpx.Client, method: str, path: str, key: str, *, headers: dict | None = None, **kwargs) -> dict:
    response = client.request(method, path, headers={"Authorization": f"Bearer {key}", **(headers or {})}, **kwargs)
    return response.raise_for_status().json()


def seed(client: httpx.Client, paths: list[Path]) -> tuple[dict, dict, dict]:
    run = uuid4().hex[:10]
    apps = {}
    for tenant in ("alpha", "beta"):
        code = f"benchmark-{tenant}-{run}"
        cli("create-tenant", code, tenant)
        cli("create-library", code, "demo", "Retrieval benchmark")
        names = ("writer", "reader", "auditor") if tenant == "alpha" else ("writer", "reader")
        for name in names:
            app = cli("register-app", code, name)
            cli("grant", code, app["id"], "demo", "write" if name == "writer" else "read")
            apps[f"{tenant}-{name}"] = app

    corpus = {}
    sources = {}
    for path in paths:
        tenant, filename = path.parent.name, path.name
        writer = apps[f"{tenant}-writer"]["key"]
        content = path.read_bytes()
        job = request(
            client, "POST", "/v1/knowledge-bases/demo/documents", writer,
            headers={"Idempotency-Key": filename, "Content-Type": "application/octet-stream"},
            params={"filename": filename}, content=content,
        )
        if (tenant, filename) == ("alpha", "audit.md"):
            request(client, "PATCH", f"/v1/documents/{job['document_id']}/readers", writer,
                    json={"readers": [apps["alpha-auditor"]["id"]]})

        published_at = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = request(client, "GET", f"/v1/ingestion-jobs/{job['job_id']}", writer)
            observed = time.perf_counter()
            if status["state"] == "failed" or status["keyword_status"] == "failed":
                raise RuntimeError(f"Fixture ingestion or indexing failed: {tenant}/{filename}")
            if status["published_version_id"] == job["version_id"]:
                if published_at is None:
                    published_at = observed
                if status["keyword_status"] == "synced":
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError(f"Fixture did not become searchable: {tenant}/{filename}")

        chunks = split_text(content.decode("utf-8-sig"), markdown=path.suffix == ".md")
        corpus[tenant, filename] = (job["version_id"], chunks)
        sources[f"{tenant}/{filename}"] = {
            **job, "sha256": hashlib.sha256(content).hexdigest(), "chunks": len(chunks),
            "publication_to_searchable_ms": (observed - published_at) * 1000,
        }
    return corpus, sources, apps


def run(client: httpx.Client, repeat: int, *, bad_cases: bool = False) -> dict:
    questions = BAD_CASES / "questions.json" if bad_cases else QUESTIONS
    raw = questions.read_bytes()
    samples = load_cases(raw, require_behavior=bad_cases)
    paths = sorted(QUESTIONS.parent.glob("corpus/*/*"))
    if bad_cases:
        paths.extend(sorted(BAD_CASES.glob("corpus/*/*")))
    corpus, sources, apps = seed(client, paths)
    expected = gold_ids(corpus, samples)
    avoid_top1 = gold_ids(corpus, [
        {**sample, "gold": sample["avoid_top1"]} for sample in samples if "avoid_top1" in sample
    ])
    evidence_sources = {}
    for sample in samples:
        gold = expected[sample["id"]]
        forbidden = avoid_top1.get(sample["id"], set())
        if gold & forbidden:
            raise ValueError(f"Gold and forbidden sources must be different chunks for {sample['id']}")
        for chunk in sorted(gold | forbidden):
            citation = request(client, "GET", f"/v1/citations/{chunk}", apps[sample["token"]]["key"])
            evidence_sources[chunk] = {
                field: citation[field] for field in ("filename", "heading", "line_start", "line_end")
            }

    def search(sample: dict, mode: str) -> dict:
        result = request(client, "POST", "/v1/retrieve", apps[sample["token"]]["key"],
                         json={**TUNING, "question": sample["question"], "mode": mode})
        if result["degraded"]:
            raise RuntimeError(f"Degraded mode {mode} for {sample['id']}: {result['degraded']}")
        return result

    for mode in MODES:
        search(samples[0], mode)
    rows = []
    rng = random.Random(42)
    for trial in range(1, repeat + 1):
        print(f"Measuring pass {trial}/{repeat}", flush=True)
        order = [(sample, mode) for sample in samples for mode in MODES]
        rng.shuffle(order)
        for sample, mode in order:
            started = time.perf_counter()
            result = search(sample, mode)
            elapsed = (time.perf_counter() - started) * 1000
            ids = [hit["chunk_id"] for hit in result["results"]]
            evidence = expected[sample["id"]]
            debug = result["debug"]
            row = {
                "id": sample["id"], "kind": sample["kind"], "mode": mode, "trial": trial,
                "http_ms": elapsed, "gold_ids": sorted(evidence), "metrics": metrics(ids, evidence),
                **debug, "ranked_ids": ids,
                "evidence_stages": evidence_stages(evidence, debug["recall"], debug["prefinal"], TUNING["top_k"]),
            }
            if forbidden := avoid_top1.get(sample["id"]):
                row["forbidden_top1_ids"] = sorted(forbidden)
                row["forbidden_top1_hit"] = bool(forbidden.intersection(ids[:1]))
            rows.append(row)
    return {
        "metadata": {
            "tuning": TUNING, "repeat": repeat, "concurrency": 1, "platform": platform.platform(),
            "suite": "bad-cases" if bad_cases else "original",
            "questions_sha256": hashlib.sha256(raw).hexdigest(), "corpus": sources,
            "embedding": EMBEDDING_MODEL, "reranker": RERANK_MODEL, "rrf_k": RRF_K,
            "chunk_chars": CHUNK_CHARS, "chunk_overlap": CHUNK_OVERLAP, "status_poll_interval_ms": 50,
        },
        "samples": samples,
        "evidence_sources": evidence_sources,
        "overall": {mode: aggregate([row for row in rows if row["mode"] == mode]) for mode in MODES},
        "requests": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark evidence retrieval through the business API.")
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--bad-cases", action="store_true", help="Run the labelled retrieval failure cases")
    parser.add_argument("--out", default=Path(".rag/benchmark.json"), type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    with httpx.Client(base_url=args.url, timeout=60, trust_env=False) as client:
        report = run(client, args.repeat, bad_cases=args.bad_cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print("| Mode | Recall@1 | Recall@3 | Recall@5 | HTTP p50 ms | HTTP p95 ms |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for mode, row in report["overall"].items():
        print(f"| {mode} | {row['recall@1']:.3f} | {row['recall@3']:.3f} | {row['recall@5']:.3f} "
              f"| {row['p50_ms']:.1f} | {row['p95_ms']:.1f} |")
    latency = [row["publication_to_searchable_ms"] for row in report["metadata"]["corpus"].values()]
    print(f"Observed publication-to-searchable p50 {statistics.median(latency):.1f} ms, max {max(latency):.1f} ms.")
    print(args.out.resolve())


if __name__ == "__main__":
    main()
