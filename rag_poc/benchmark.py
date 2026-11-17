import argparse
import hashlib
import json
import math
import platform
import random
import statistics
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid5

import httpx

from .documents import CHUNK_CHARS, CHUNK_OVERLAP, split_document
from .retrieval import MODES

ROOT = Path(__file__).resolve().parent.parent / "poc"
BASE_URL = "http://127.0.0.1:8000"
TOP_K = 5
TUNING = {"top_k": TOP_K, "candidates": 8, "rerank_limit": 12, "debug": True}


def request(client: httpx.Client, method: str, path: str, token: str, **kwargs) -> httpx.Response:
    response = client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    response.raise_for_status()
    return response


def search(client: httpx.Client, token: str, question: str, mode: str, **kwargs) -> dict:
    return request(client, "POST", "/v1/retrieve", token, json={
        "question": question, "mode": mode, **(TUNING | kwargs),
    }).json()


def upload(client: httpx.Client, token: str, filename: str, content: str, readers: list[str]) -> dict:
    return request(client, "POST", "/v1/knowledge-bases/demo/documents", token, json={
        "filename": filename, "content": content, "readers": readers,
    }).json()


def seed(client: httpx.Client) -> tuple[dict, dict]:
    corpus = {}
    stats = {}
    for path in sorted((ROOT / "corpus").glob("*/*")):
        tenant, filename = path.parent.name, path.name
        content = path.read_text()
        readers = ["auditor"] if (tenant, filename) == ("alpha", "audit.md") else []
        result = upload(client, f"{tenant}-admin", filename, content, readers)
        chunks = split_document(filename, content)
        if not result["keyword_ready"] or result["chunk_count"] != len(chunks):
            raise RuntimeError(f"Could not publish the full fixture to both indexes: {path}")
        corpus[tenant, filename] = (result["version_id"], chunks)
        stats[f"{tenant}/{filename}"] = {
            **result, "readers": readers, "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
    return corpus, stats


def gold_ids(corpus: dict, samples: list[dict]) -> dict[str, set[str]]:
    gold = {}
    for sample in samples:
        tenant = sample["token"].split("-")[0]
        ids = set()
        for filename, anchor in sample["gold"]:
            version_id, chunks = corpus[tenant, filename]
            matches = [i for i, chunk in enumerate(chunks) if anchor in chunk.body]
            if len(matches) != 1:
                raise ValueError(f"Gold anchor must occur in one chunk: {sample['id']} {filename} {anchor}")
            ids.add(str(uuid5(UUID(version_id), str(matches[0]))))
        if not ids:
            raise ValueError(f"Sample has no gold evidence: {sample['id']}")
        gold[sample["id"]] = ids
    return gold


def metrics(ids: list[str], gold: set[str]) -> dict[str, float]:
    hits = [chunk_id in gold for chunk_id in ids]
    dcg = sum(hit / math.log2(rank + 1) for rank, hit in enumerate(hits, 1))
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(TOP_K, len(gold)) + 1))
    return {
        **{f"recall@{k}": sum(hits[:k]) / len(gold) for k in (1, 3, TOP_K)},
        f"hit@{TOP_K}": float(any(hits)),
        f"mrr@{TOP_K}": next((1 / rank for rank, hit in enumerate(hits, 1) if hit), 0),
        f"ndcg@{TOP_K}": dcg / ideal,
    }


def aggregate(rows: list[dict]) -> dict:
    scores = {name: statistics.mean(row["metrics"][name] for row in rows) for name in rows[0]["metrics"]}
    latency = sorted(row["http_ms"] for row in rows)
    stages = {
        key: statistics.median(row["stage_ms"][key] for row in rows if key in row["stage_ms"])
        for key in rows[0]["stage_ms"]
    }
    return {
        **scores, "p50_ms": statistics.median(latency), "p95_ms": latency[math.ceil(0.95 * len(latency)) - 1],
        "stage_p50_ms": stages, "requests": len(rows),
    }


def coverage(rows: list[dict]) -> dict:
    result = []
    for row in rows:
        if row["mode"] != "rrf":
            continue
        gold = set(row["gold_ids"])
        vector, keyword = (set(row["recall"][branch]) for branch in ("vector", "keyword"))
        result.append({
            "union_recall": len(gold & (vector | keyword)) / len(gold),
            "vector_only_gold": len(gold & (vector - keyword)),
            "keyword_only_gold": len(gold & (keyword - vector)),
            "overlap": len(vector & keyword),
            "shortlist_recall": len(gold & set(row["prefinal"][:TUNING["rerank_limit"]])) / len(gold),
        })
    return {name: statistics.mean(row[name] for row in result) for name in result[0]}


def run(client: httpx.Client, repeat: int) -> dict:
    sample_file = (ROOT / "questions.json").read_bytes()
    samples = json.loads(sample_file)
    corpus, corpus_stats = seed(client)
    gold = gold_ids(corpus, samples)
    for sample in samples:
        for chunk_id in gold[sample["id"]]:
            request(client, "GET", f"/v1/citations/{chunk_id}", sample["token"],
                    params={"knowledge_base_id": "demo"})
    settings = client.get("/health").raise_for_status().json()
    # Warm pooled connections, both ONNX sessions and the four request paths.
    for mode in MODES:
        search(client, samples[0]["token"], samples[0]["question"], mode)

    rows = []
    rng = random.Random(42)
    for trial in range(repeat):
        print(f"Measured pass {trial + 1}/{repeat}", flush=True)
        jobs = [(sample, mode) for sample in samples for mode in MODES]
        rng.shuffle(jobs)
        for sample, mode in jobs:
            started = perf_counter()
            result = search(client, sample["token"], sample["question"], mode)
            latency = (perf_counter() - started) * 1000
            if result["degraded"]:
                raise RuntimeError(f'Degraded run: {sample["id"]} {mode} {result["degraded"]}')
            ids = [row["chunk_id"] for row in result["results"]]
            rows.append({
                "id": sample["id"], "kind": sample["kind"], "mode": mode, "trial": trial + 1,
                "gold_ids": sorted(gold[sample["id"]]), "metrics": metrics(ids, gold[sample["id"]]),
                "http_ms": latency, **result["debug"], "ranked_evidence": result["results"],
            })

    return {
        "metadata": {
            **settings, "tuning": TUNING, "concurrency": 1, "repeat": repeat,
            "platform": platform.platform(), "python": platform.python_version(),
            "onnxruntime": version("onnxruntime"), "chunk_chars": CHUNK_CHARS, "overlap": CHUNK_OVERLAP,
            "questions_sha256": hashlib.sha256(sample_file).hexdigest(), "corpus": corpus_stats,
        },
        "samples": samples,
        "overall": {mode: aggregate([row for row in rows if row["mode"] == mode]) for mode in MODES},
        "by_kind": {
            kind: {mode: aggregate([row for row in rows if row["mode"] == mode and row["kind"] == kind])
                   for mode in MODES}
            for kind in sorted({sample["kind"] for sample in samples})
        },
        "coverage": coverage(rows), "requests": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare four retrieval modes on the same labelled evidence.")
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path(".poc/results.json"))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    with httpx.Client(base_url=args.url, timeout=60, trust_env=False) as client:
        report = run(client, args.repeat)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print("| mode | Recall@1 | Recall@3 | Recall@5 | nDCG@5 | p50 ms | p95 ms |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for mode, row in report["overall"].items():
        print(f"| {mode} | {row['recall@1']:.3f} | {row['recall@3']:.3f} | {row['recall@5']:.3f} "
              f"| {row['ndcg@5']:.3f} | {row['p50_ms']:.1f} | {row['p95_ms']:.1f} |")
    print(args.out.resolve())


if __name__ == "__main__":
    main()
