import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from time import monotonic, sleep
from uuid import UUID, uuid5

import httpx

from .answer import AnswerRequest
from .benchmark import cli, request
from .models import EMBEDDING_MODEL, RERANK_MODEL
from .policy import Policy
from .retrieval import RRF_K, Search
from .spans import SourceSpan
from .text import split_text

CASES_FILE = Path(__file__).with_name("answer_cases.json")


def seed(client, fixtures):
    cases = fixtures["cases"]
    users = {}
    manifest = {}
    for tenant, libraries in (
        (name, sorted({case["library"] for case in cases if case["tenant"] == name}))
        for name in ("alpha", "beta")
    ):
        code = f"answer-{tenant}"
        cli("create-tenant", code, code)
        writer = cli("register-app", code, "Writer")
        reader = cli("register-app", code, "Reader")
        for library in libraries:
            cli("create-library", code, library, library)
            cli("grant", code, writer["id"], library, "write")
            cli("grant", code, reader["id"], library, "read")
        users[tenant] = (writer["key"], reader["key"])
    writer = users["alpha"][0]
    for doc in fixtures["documents"]:
        original = doc["text"].encode()
        library, name = doc["library"], doc["id"]
        job = request(client, "POST", f"/v1/knowledge-bases/{library}/documents", writer,
                      params={"filename": f"{name}.md"},
                      headers={"Idempotency-Key": name, "Content-Type": "application/octet-stream"},
                      content=original)
        if doc["restricted"]:
            request(client, "PATCH", f"/v1/documents/{job['document_id']}/readers", writer, json={"readers": []})
        deadline = monotonic() + 75
        while True:
            status = request(client, "GET", f"/v1/ingestion-jobs/{job['job_id']}", writer)
            if status["state"] == "failed" or status["keyword_status"] == "failed" or monotonic() >= deadline:
                raise RuntimeError(f"Unable to seed {name}: {status['state']}/{status['keyword_status']}")
            if status["keyword_status"] == "synced":
                break
            sleep(0.08)
        manifest[name] = {**job, "sha256": hashlib.sha256(original).hexdigest(), "library": library,
                          "restricted": doc["restricted"]}
    return users, manifest


def ranges(fixtures, manifest):
    overall = {}
    documents = {doc["id"]: doc["text"] for doc in fixtures["documents"]}
    for key, text in documents.items():
        meta = manifest[key]
        for ordinal, chunk in enumerate(split_text(text, markdown=True)):
            overall[str(uuid5(UUID(meta["version_id"]), str(ordinal)))] = SourceSpan(
                key, meta["sha256"], chunk.start_offset, chunk.end_offset,
            )
    return overall, documents


def measure(case, response, manifest, spans, documents, checked):
    targets = []
    for document, quote in case["gold"]:
        text = documents[document]
        start = text.index(quote)
        if text.find(quote, start + 1) != -1:
            raise ValueError(f"Non-unique answer gold quote in {case['id']}")
        targets.append(SourceSpan(document, manifest[document]["sha256"], start, start + len(quote)))
    context = response["context"] or []
    context_by_id = {item["evidence_id"]: item for item in context}
    claims = response["claims"]
    citations = {item["chunk_id"]: item for item in response["citations"]}
    supporting_ids = {identifier for claim in claims for identifier in claim["evidence_ids"]}
    expected_citations = supporting_ids if claims else set(citations) if response["status"] == "policy_conflict" else set()
    bound = all(claim["text"] in context_by_id.get(identifier, {}).get("text", "")
                for claim in claims for identifier in claim["evidence_ids"])
    actual_ids = set(context_by_id) | set(citations) | {i for round_ in response["rounds"] for i in round_["evidence_ids"]}
    authorized = all(
        chunk in checked and chunk in spans and case["tenant"] == "alpha"
        and not manifest[spans[chunk].document]["restricted"]
        and manifest[spans[chunk].document]["library"] == case["library"]
        and checked[chunk]["version_id"] == manifest[spans[chunk].document]["version_id"]
        and checked[chunk]["document_id"] == manifest[spans[chunk].document]["document_id"]
        and (chunk not in citations or all(
            citations[chunk][field] == checked[chunk][field]
            for field in ("version_id", "document_id", "line_start", "line_end")
        ))
        for chunk in actual_ids
    )
    day = response["effective_date"]
    if day:
        from datetime import date
        applicable = all(not item.get("policy_data") or Policy.model_validate(item["policy_data"]).applies(
            date.fromisoformat(day)
        ) for item in checked.values())
    else:
        applicable = not actual_ids
    if case.get("as_of") and response["status"] != "needs_clarification":
        applicable = applicable and day == case["as_of"]
    coverage = {}
    first_round = response["rounds"][0]["evidence_ids"] if response["rounds"] else []
    for stage, ids in (("first", first_round), ("context", context_by_id), ("citations", citations)):
        pieces = [spans[chunk] for chunk in ids if chunk in spans]
        coverage[stage] = mean(target.covers(pieces) for target in targets) if targets else None
    return {
        "status": response["status"] == case["expected_status"],
        "citations": bound and supporting_ids <= set(context_by_id) and set(citations) == expected_citations,
        "authorization": authorized,
        "policy": applicable,
        "budget": sum(response["model_calls"].values()) <= 6 and len(response["rounds"]) <= 2
        and sum(round_["context_chars"] for round_ in response["rounds"]) <= 4000
        and len(json.dumps(context, separators=(",", ":"), ensure_ascii=False)) <= 4000
        and response["elapsed_ms"] <= 16250,
        "facts": all(fact in "\n".join(claim["text"] for claim in claims) for fact in case["facts"]),
        "coverage": coverage,
    }


def grouped(rows):
    groups = {}
    for row in rows:
        strategy, split, group = row["strategy"], row["case"]["split"], row["case"]["group"]
        for label in ((strategy,), (strategy, split), (strategy, split, group)):
            groups.setdefault("/".join(label), []).append(row)
    results = {}
    for label, group in sorted(groups.items()):
        latency = sorted(r["http_ms"] for r in group)
        results[label] = {
            "requests": len(group),
            "all_checks_pass": sum(all(entry for key, entry in r["checks"].items() if key != "coverage") for r in group),
            "full_cited_evidence": sum(r["checks"]["coverage"]["citations"] == 1 for r in group),
            "http_p95_ms": round(latency[math.ceil(0.95 * len(latency)) - 1], 2),
            "model_calls": mean(sum(r["answer"]["model_calls"].values()) for r in group),
            "generation_tokens": {name: mean(r["answer"]["token_usage"].get(name, 0) for r in group)
                                  for name in ("prompt_tokens", "completion_tokens")},
        }
    return results


def compare(rows):
    pairs = {}
    for row in rows:
        pairs.setdefault((row["case"]["id"], row["trial"]), {})[row["strategy"]] = row
    results = []
    for (case, trial), pair in pairs.items():
        if set(pair) != {"fixed", "agentic"}:
            continue
        fixed, agentic = pair["fixed"], pair["agentic"]
        results.append({"id": case, "trial": trial, "split": fixed["case"]["split"],
                        "group": fixed["case"]["group"], "fixed": fixed["checks"], "agentic": agentic["checks"],
                        "http_delta_ms": round(agentic["http_ms"] - fixed["http_ms"], 2),
                        "extra_model_calls": sum(agentic["answer"]["model_calls"].values())
                        - sum(fixed["answer"]["model_calls"].values()),
                        "extra_generation_tokens": {name: agentic["answer"]["token_usage"].get(name, 0)
                                                    - fixed["answer"]["token_usage"].get(name, 0)
                                                    for name in ("prompt_tokens", "completion_tokens")}})
    return results


def collect(client, split, paired, repeat):
    raw = CASES_FILE.read_bytes()
    fixtures = json.loads(raw)
    users, manifest = seed(client, fixtures)
    spans, documents = ranges(fixtures, manifest)
    cases = [case for case in fixtures["cases"] if split in ("all", case["split"])]
    rows = []
    for trial in range(1, repeat + 1):
        for position, case in enumerate(cases):
            key = users[case["tenant"]][1]
            choices = ("fixed", "agentic") if paired else ("fixed",)
            if (position + trial) % 2:
                choices = choices[::-1]
            for strategy in choices:
                payload = {"knowledge_base_code": case["library"], "question": case["question"],
                           "strategy": strategy, "include_context": True}
                if case.get("as_of"):
                    payload["as_of"] = case["as_of"]
                AnswerRequest.model_validate(payload)
                started = monotonic()
                result = request(client, "POST", "/v1/answer", key, json=payload)
                http_ms = (monotonic() - started) * 1000
                if result["strategy"] != strategy:
                    raise RuntimeError(f"Requested strategy {strategy} but API ran {result['strategy']}")
                ids = {r["evidence_id"] for r in result["context"] or []} | {
                    r["chunk_id"] for r in result["citations"]
                } | {identifier for round_ in result["rounds"] for identifier in round_["evidence_ids"]}
                checked = {}
                for identifier in sorted(ids):
                    source = client.get(f"/v1/citations/{identifier}", headers={"Authorization": f"Bearer {key}"})
                    if source.status_code == 200:
                        checked[identifier] = source.json()
                scores = measure(case, result, manifest, spans, documents, checked)
                rows.append({"case": case, "strategy": strategy, "trial": trial, "http_ms": http_ms,
                             "checks": scores, "answer": result})
                print(f"{case['id']} {strategy} {result['status']} {result['reason']}", flush=True)
    retrieval = Search(knowledge_base_code="evaluation", question="evaluation", mode="rerank", expand_neighbors=True)
    settings = {name: getattr(retrieval, name) for name in ("mode", "top_k", "candidates", "rerank_limit", "expand_neighbors")}
    settings.update({name: AnswerRequest.model_fields[name].default
                     for name in ("deadline_ms", "max_model_calls", "max_context_chars")})
    settings.update(rrf_k=RRF_K, embedding_model=EMBEDDING_MODEL, rerank_model=RERANK_MODEL,
                    generation_models=sorted({row["answer"]["model"] for row in rows if row["answer"]["model"]}))
    return {"dataset_sha256": hashlib.sha256(raw).hexdigest(), "settings": settings,
            "sources": manifest, "results": rows, "groups": grouped(rows), "pairs": compare(rows)}


def main():
    parser = argparse.ArgumentParser(description="Collect authorized synthetic QA answers for offline evaluation")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    parser.add_argument("--paired", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    with httpx.Client(base_url=args.url, timeout=40, trust_env=False) as client:
        report = collect(client, args.split, args.paired, args.repeat)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(args.out.resolve())
    for row in report["results"]:
        if not all(row["checks"][name] for name in ("citations", "authorization", "policy", "budget")):
            parser.exit(1, "Deterministic answer safety checks failed\n")


if __name__ == "__main__":
    main()
