import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from poc.benchmark import aggregate, gold_ids, metrics

from .models import EMBEDDING_MODEL, RERANK_MODEL
from .replay import evidence_ranks, replay
from .retrieval import BRANCHES, RRF_K
from .spans import SourceSpan
from .text import CHUNK_CHARS, CHUNK_OVERLAP, split_text

QUESTIONS = Path(__file__).resolve().parents[1] / "poc" / "questions.json"
BAD_CASES = Path(__file__).with_name("benchmark_cases")
MODES = tuple(BRANCHES)
TUNING = {"knowledge_base_code": "demo", "top_k": 5, "candidates": 8, "rerank_limit": 12, "debug": True}
PAIRED = {
    "rrf-8": {"mode": "rrf", "candidates": 8, "rerank_limit": 12},
    "rrf-50": {"mode": "rrf", "candidates": 50, "rerank_limit": 12},
    "rerank-8-12": {"mode": "rerank", "candidates": 8, "rerank_limit": 12},
    "rerank-50-12": {"mode": "rerank", "candidates": 50, "rerank_limit": 12},
    "rerank-50-30": {"mode": "rerank", "candidates": 50, "rerank_limit": 30},
}
EVIDENCE_CASES = {
    "Q01": {"interpret": True}, "Q03": {"interpret": True},
    "Q09": {"interpret": True}, "Q10": {"interpret": True},
    "Q08": {"as_of": "2026-11-17"}, "Q18": {"as_of": "2026-11-17"},
    "Q15": {"as_of": "2026-08-31"}, "Q16": {"as_of": "2026-09-01"}, "Q17": {"as_of": "2026-09-02"},
    "Q19": {"as_of": "2026-11-17"},
}
CaseText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EvidenceAnchors = Annotated[list[tuple[CaseText, CaseText]], Field(min_length=1)]


class GoldRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: CaseText
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: CaseText
    token: Literal["alpha-reader", "alpha-auditor", "beta-reader"]
    kind: CaseText
    question: Annotated[CaseText, StringConstraints(max_length=240)]
    gold: EvidenceAnchors
    gold_spans: Annotated[list[GoldRange], Field(min_length=1)] | None = None
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


def evidence_case_passed(row: dict) -> bool:
    case = row["id"]
    hints = row["query_hints"]
    conflicts = row["conflict_sources"]
    if case in ("Q08", "Q18"):
        return set(row["gold_ids"]) <= set(conflicts)
    if conflicts:
        return False
    if case == "Q01":
        return hints.get("suggested_identifier") == "GW-4102" and not row["needs_clarification"]
    if case == "Q09":
        return row["needs_clarification"] and hints.get("possible_identifiers") == ["GW-4102", "GW-4103"]
    if case == "Q03":
        return row["needs_clarification"] and set(hints.get("possible_scopes", [])) == {"普通商品", "冷藏生鲜"}
    return row["metrics"]["recall@1"] == 1 and not row.get("forbidden_top1_hit") and not row["needs_clarification"]


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


def run(client: httpx.Client, repeat: int, *, bad_cases: bool = False, paired: bool = False,
        evidence_only: bool = False) -> dict:
    questions = BAD_CASES / "questions.json" if bad_cases else QUESTIONS
    raw = questions.read_bytes()
    samples = load_cases(raw, require_behavior=bad_cases)
    if evidence_only:
        samples = [sample for sample in samples if sample["id"] in EVIDENCE_CASES]
    paths = sorted(QUESTIONS.parent.glob("corpus/*/*"))
    if bad_cases:
        paths.extend(sorted(BAD_CASES.glob("corpus/*/*")))
    corpus, sources, apps = seed(client, paths)
    originals = {f"{path.parent.name}/{path.name}": path.read_bytes().decode("utf-8-sig") for path in paths}
    chunk_spans = {}
    for (tenant, filename), (version, chunks) in corpus.items():
        doc = f"{tenant}/{filename}"
        for ordinal, chunk in enumerate(chunks):
            chunk_spans[str(uuid5(UUID(version), str(ordinal)))] = SourceSpan(
                doc, sources[doc]["sha256"], chunk.start_offset, chunk.end_offset,
            )
    gold_spans = {}
    expected = {}
    for sample in samples:
        targets = []
        tenant = sample["token"].split("-")[0]
        if "gold_spans" in sample:
            for item in sample["gold_spans"]:
                span = SourceSpan(**item)
                if (not span.document.startswith(f"{tenant}/") or span.document not in sources
                    or sources[span.document]["sha256"] != span.sha256
                    or not span.start < span.end <= len(originals[span.document])):
                    raise ValueError(f"Gold range does not match the source version for {sample['id']}")
                targets.append(span)
        else:
            for filename, anchor in sample["gold"]:
                doc = f"{tenant}/{filename}"
                original = originals[doc]
                start = original.find(anchor)
                if start < 0 or original.find(anchor, start + 1) >= 0:
                    raise ValueError(f"Gold anchor must identify one original range for {sample['id']}: {anchor}")
                targets.append(SourceSpan(doc, sources[doc]["sha256"], start, start + len(anchor)))
        gold_spans[sample["id"]] = targets
        expected[sample["id"]] = {
            chunk for chunk, piece in chunk_spans.items()
            if any((piece.document, piece.sha256) == (target.document, target.sha256)
                   and piece.start < target.end and target.start < piece.end for target in targets)
        }
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

    profiles = PAIRED if paired else {mode: {"mode": mode} for mode in MODES}

    def search(sample: dict, parameters: dict) -> dict:
        result = request(client, "POST", "/v1/retrieve", apps[sample["token"]]["key"],
                         json={**parameters, "question": sample["question"]})
        if result["degraded"]:
            raise RuntimeError(f"Degraded mode {parameters['mode']} for {sample['id']}: {result['degraded']}")
        return result

    for profile in profiles.values():
        search(samples[0], TUNING | profile)
    rows = []
    rng = random.Random(42)
    for trial in range(1, repeat + 1):
        print(f"Measuring pass {trial}/{repeat}", flush=True)
        order = [(sample, profile) for sample in samples for profile in profiles]
        rng.shuffle(order)
        for sample, profile in order:
            parameters = TUNING | profiles[profile]
            if evidence_only:
                parameters |= EVIDENCE_CASES[sample["id"]]
            started = time.perf_counter()
            result = search(sample, parameters)
            elapsed = (time.perf_counter() - started) * 1000
            ids = [hit["chunk_id"] for hit in result["results"]]
            evidence = expected[sample["id"]]
            spans = gold_spans[sample["id"]]
            pieces = [chunk_spans[chunk] for chunk in ids]
            debug = result["debug"]
            row = {
                "id": sample["id"], "kind": sample["kind"], "mode": parameters["mode"], "trial": trial,
                "profile": profile, "parameters": parameters,
                "http_ms": elapsed, "gold_ids": sorted(evidence), "metrics": metrics(ids, evidence),
                **debug, "ranked_ids": ids,
                "evidence_stages": evidence_stages(evidence, debug["recall"], debug["prefinal"], TUNING["top_k"]),
                "all_gold_in_top5": all(span.covers(pieces) for span in spans),
                "span_recall_at_5": statistics.mean(span.covers(pieces) for span in spans),
                "first_source": ({field: result["results"][0][field] for field in
                                  ("chunk_id", "filename", "heading", "line_start", "line_end")} if ids else None),
            }
            if forbidden := avoid_top1.get(sample["id"]):
                row["forbidden_top1_ids"] = sorted(forbidden)
                row["forbidden_top1_hit"] = bool(forbidden.intersection(ids[:1]))
            if evidence_only:
                row["needs_clarification"] = result.get("needs_clarification", False)
                row["query_hints"] = result.get("query_hints", {})
                row["conflict_sources"] = [item["chunk_id"] for item in result.get("policy_conflicts", [])]
                row["expected_behavior_passed"] = evidence_case_passed(row)
            row["evidence_ranks"] = evidence_ranks(row, evidence_sources, {
                **debug["recall"], "fusion": debug["prefinal"], "rerank_input": debug["rerank_input"], "top5": ids,
            })
            rows.append(row)
    overall = {}
    for profile in profiles:
        group = [row for row in rows if row["profile"] == profile]
        overall[profile] = {
            **aggregate(group),
            "all_gold_in_top5": statistics.mean(row["all_gold_in_top5"] for row in group),
            "span_recall_at_5": statistics.mean(row["span_recall_at_5"] for row in group),
            "forbidden_top1_count": sum(row.get("forbidden_top1_hit", False) for row in group),
            "model_calls": {name: sum(row["model_calls"][name] for row in group) for name in ("embedding", "rerank")},
        }
    return {
        "metadata": {
            "tuning": TUNING, "repeat": repeat, "concurrency": 1, "platform": platform.platform(),
            "suite": "evidence" if evidence_only else ("bad-cases" if bad_cases else "original"), "profiles": profiles,
            "questions_sha256": hashlib.sha256(raw).hexdigest(), "corpus": sources,
            "embedding": EMBEDDING_MODEL, "reranker": RERANK_MODEL, "rrf_k": RRF_K,
            "chunk_chars": CHUNK_CHARS, "chunk_overlap": CHUNK_OVERLAP, "status_poll_interval_ms": 50,
        },
        "samples": samples,
        "evidence_sources": evidence_sources,
        "gold_spans": {case: [asdict(span) for span in spans] for case, spans in gold_spans.items()},
        "overall": overall,
        "requests": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark evidence retrieval through the business API.")
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--bad-cases", action="store_true", help="Run the labelled retrieval failure cases")
    study = parser.add_mutually_exclusive_group()
    study.add_argument("--paired", action="store_true", help="Compare candidate budgets and reranking windows on one index")
    study.add_argument("--evidence", action="store_true", help="Run explicit date and clarification cases")
    study.add_argument("--replay", type=Path, help="Replay an existing benchmark report without API calls")
    parser.add_argument("--rrf-k", type=int, nargs="+", default=[1, 60])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.replay:
        if any(k < 0 for k in args.rrf_k):
            parser.error("--rrf-k must be nonnegative")
        report = replay(json.loads(args.replay.read_text()), args.rrf_k)
        out = args.out or Path(".rag/replay.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(f"Verified {report['verified_fusion_rows']} recorded rankings. {out.resolve()}")
        return
    args.out = args.out or Path(".rag/benchmark.json")
    with httpx.Client(base_url=args.url, timeout=60, trust_env=False) as client:
        report = run(client, args.repeat, bad_cases=args.bad_cases or args.evidence, paired=args.paired,
                     evidence_only=args.evidence)
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
    if args.evidence:
        failures = sorted({f"{row['id']}/{row['mode']}" for row in report["requests"]
                           if not row["expected_behavior_passed"]})
        if failures:
            parser.exit(1, f"Evidence acceptance failed: {', '.join(failures)}\n")


if __name__ == "__main__":
    main()
