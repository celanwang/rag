"""Compare the native RAGFlow API with the self-built retrieval API."""
import argparse
import hashlib
import html
import json
import random
import re
import unicodedata
from pathlib import Path
from time import monotonic, perf_counter, sleep

import httpx

from .benchmark import BASE_URL, ROOT, TOP_K, TUNING, aggregate, search, seed
from .ragflow_embedding import MODEL_ID

RF_URL = "http://127.0.0.1:19380"
MODES = ("ragflow_native", "poc_rrf", "poc_rerank")
NATIVE_SETTINGS = {
    "page": 1,
    "page_size": TOP_K,
    "similarity_threshold": 0.2,
    "keywords_similarity_weight": 0.7,
    "knn_top_k": 1024,
    "knn_num_candidates": 2048,
    "rerank_candidates_count": 64,
    "include_knowledge_compilation": False,
}


def data(response: httpx.Response):
    response.raise_for_status()
    payload = response.json()
    if payload["code"] != 0:
        raise RuntimeError(f"{response.request.method} {response.request.url.path}: {payload.get('message')}")
    return payload.get("data")


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class RAGFlow:
    def __init__(self, client: httpx.Client, config: dict):
        self.client = client
        self.config = config

    def seed(self) -> tuple[dict, dict]:
        datasets = {}
        documents = {}
        for tenant in ("alpha", "beta"):
            config = self.config[tenant]
            headers = auth(config["key"])
            name = f"rag-poc-baseline-{tenant}"
            matches = data(self.client.get("/api/v1/datasets", headers=headers, params={"name": name}))
            existing = [row for row in matches if row["name"] == name]
            dataset = existing[0] if existing else data(self.client.post("/api/v1/datasets", headers=headers, json={
                "name": name, "parse_type": 1, "parser_id": "general", "language": "Chinese",
                "permission": "me", "embedding_model": config["embedding_model"],
            }))
            datasets[tenant] = dataset
            location = f'/api/v1/datasets/{dataset["id"]}'
            if dataset["embedding_model"] != config["embedding_model"]:
                raise RuntimeError(f"Unexpected embedding model in {name}")
            paths = {path.name: path for path in (ROOT / "corpus" / tenant).iterdir() if path.is_file()}
            listed = self.documents(location, headers)
            if len(listed) != len({row["name"] for row in listed}) or {row["name"] for row in listed} - paths.keys():
                raise RuntimeError(f"Unexpected documents in the dedicated dataset {name}")
            loaded = {row["name"]: row for row in listed}
            to_upload = [(filename, path.read_bytes()) for filename, path in sorted(paths.items()) if filename not in loaded]
            if to_upload:
                data(self.client.post(f"{location}/documents", headers=headers, files=[
                    ("file", (filename, content, "text/plain")) for filename, content in to_upload
                ]))
                loaded = {row["name"]: row for row in self.documents(location, headers)}
            pending = [row["id"] for row in loaded.values()
                       if row["ingestion_status"] in {"UNSTART", "CANCELLED", "FAILED"}]
            if pending:
                data(self.client.post(f"{location}/chunks", headers=headers, json={"document_ids": pending}))
            docs = self.wait_for_parse(location, headers, set(paths))
            for row in docs:
                filename = row["name"]
                download = self.client.get(f'{location}/documents/{row["id"]}', headers=headers).raise_for_status()
                original = paths[filename].read_bytes()
                if download.content != original:
                    raise RuntimeError(f"RAGFlow source bytes differ from fixture {tenant}/{filename}")
                row["sha256"] = hashlib.sha256(original).hexdigest()
            documents[tenant] = {row["name"]: row for row in docs}
            print(f"RAGFlow {tenant}: {len(docs)} files, {sum(row['chunk_count'] for row in docs)} chunks", flush=True)
        return datasets, documents

    def documents(self, location: str, headers: dict) -> list[dict]:
        return data(self.client.get(f"{location}/documents", headers=headers))["docs"]

    def wait_for_parse(self, location: str, headers: dict, filenames: set[str]) -> list[dict]:
        start = monotonic()
        while monotonic() - start < 180:
            docs = self.documents(location, headers)
            failures = {row["name"]: row.get("progress_msg") for row in docs if row["ingestion_status"] == "FAILED"}
            if failures:
                raise RuntimeError(f"RAGFlow parsing failed: {failures}")
            complete = {row["name"] for row in docs
                        if row["ingestion_status"] == "COMPLETED" and row["chunk_count"] > 0}
            if complete == filenames:
                return docs
            sleep(2)
        raise TimeoutError(f"RAGFlow did not finish parsing {location}: {sorted(filenames - complete)}")

    def call(self, key: str, datasets: list[str], question: str, **overrides) -> httpx.Response:
        return self.client.post("/api/v1/retrieval", headers=auth(key), json={
            **NATIVE_SETTINGS, "dataset_ids": datasets, "question": question, **overrides,
        })


def text_key(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", "", text))).casefold().split())


def evidence_metrics(chunks: list[dict], gold: list[list[str]]) -> dict[str, float]:
    matches = []
    for chunk in chunks:
        body = text_key(chunk["body"])
        matches.append({index for index, (filename, anchor) in enumerate(gold)
                        if filename == chunk["filename"] and text_key(anchor) in body})
    return {
        **{f"recall@{k}": len(set().union(*matches[:k])) / len(gold) for k in (1, 3, TOP_K)},
        f"hit@{TOP_K}": float(any(matches)),
        f"mrr@{TOP_K}": next((1 / rank for rank, hit in enumerate(matches, 1) if hit), 0),
    }


def indexed_evidence(ragflow: RAGFlow, datasets: dict, documents: dict, samples: list[dict]) -> dict:
    indexed = {}
    for tenant, files in documents.items():
        dataset_id = datasets[tenant]["id"]
        headers = auth(ragflow.config[tenant]["key"])
        for filename, document in files.items():
            path = f'/api/v1/datasets/{dataset_id}/documents/{document["id"]}/chunks'
            chunks = data(ragflow.client.get(path, headers=headers, params={"page_size": 100}))["chunks"]
            indexed[tenant, filename] = [text_key(chunk["content"]) for chunk in chunks if chunk["available"]]
    missing = []
    for sample in samples:
        tenant = sample["token"].split("-")[0]
        for filename, anchor in sample["gold"]:
            if not any(text_key(anchor) in text for text in indexed[tenant, filename]):
                missing.append({"id": sample["id"], "filename": filename, "anchor": anchor})
    return {"gold_count": sum(len(sample["gold"]) for sample in samples), "missing": missing}


def compact_probe(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    details = payload.get("data")
    chunks = details.get("chunks", []) if isinstance(details, dict) else []
    return {
        "http": response.status_code, "code": payload.get("code"), "message": payload.get("message"),
        "documents": sorted({row["document_id"] for row in chunks}),
    }


def isolation_probes(ragflow: RAGFlow, poc: httpx.Client, datasets: dict, documents: dict) -> list[dict]:
    probes = []
    question = "GW-4102"
    for tenant, foreign in (("alpha", "beta"), ("beta", "alpha")):
        key = ragflow.config[tenant]["key"]
        own = datasets[tenant]["id"]
        other = datasets[foreign]["id"]
        foreign_doc = documents[foreign]["support.md"]["id"]
        targets = [
            ("foreign_dataset", [other], {}),
            ("mixed_datasets", [own, other], {}),
            ("foreign_document_in_own_dataset", [own], {"document_ids": [foreign_doc]}),
        ]
        for case, scope, overrides in targets:
            result = compact_probe(ragflow.call(key, scope, question, **overrides))
            foreign_ids = {row["id"] for row in documents[foreign].values()}
            probes.append({"system": "ragflow", "caller": tenant, "case": case, **result,
                           "exposed": bool(set(result["documents"]) & foreign_ids)})
        path = f"/api/v1/datasets/{other}/documents/{foreign_doc}"
        response = ragflow.client.get(path, headers=auth(key))
        has_file = response.content == (ROOT / "corpus" / foreign / "support.md").read_bytes()
        probes.append({"system": "ragflow", "caller": tenant, "case": "foreign_download",
                       **compact_probe(response), "exposed": has_file})

        asserted = poc.post("/v1/retrieve", headers=auth(f"{tenant}-reader"),
                            json={"question": question, "tenant_id": foreign, "mode": "rrf"})
        probes.append({"system": "poc", "caller": tenant, "case": "forged_tenant_field",
                       "http": asserted.status_code, "exposed": bool(asserted.json().get("results"))})
        target = search(poc, f"{foreign}-reader", question, "rrf")["results"][0]
        citation = poc.get(f'/v1/citations/{target["chunk_id"]}', headers=auth(f"{tenant}-reader"),
                           params={"knowledge_base_id": "demo"})
        probes.append({"system": "poc", "caller": tenant, "case": "foreign_citation",
                       "http": citation.status_code, "exposed": citation.is_success})

    question = "BIOSEAL-9151 AU-3368"
    alpha = datasets["alpha"]["id"]
    audit_id = documents["alpha"]["audit.md"]["id"]
    key = ragflow.config["alpha"]["key"]
    allowed = [row["id"] for filename, row in documents["alpha"].items() if filename != "audit.md"]
    for case, overrides in [
        ("reader_allowlist", {"document_ids": allowed}),
        ("reader_removed_allowlist", {}),
        ("reader_supplied_audit_document", {"document_ids": [audit_id]}),
    ]:
        result = compact_probe(ragflow.call(key, [alpha], question, **overrides))
        probes.append({"system": "ragflow", "caller": "alpha-reader", "case": case, **result,
                       "exposed": audit_id in result["documents"]})
    download = ragflow.client.get(f"/api/v1/datasets/{alpha}/documents/{audit_id}", headers=auth(key))
    probes.append({"system": "ragflow", "caller": "alpha-reader", "case": "reader_audit_download",
                   **compact_probe(download), "exposed": b"BIOSEAL-9151" in download.content})
    audit = search(poc, "alpha-auditor", question, "rrf")
    audit_chunk_id = next(row["chunk_id"] for row in audit["results"] if "BIOSEAL-9151" in row["body"])
    reader = search(poc, "alpha-reader", question, "rrf")
    citation = poc.get(f"/v1/citations/{audit_chunk_id}", headers=auth("alpha-reader"),
                       params={"knowledge_base_id": "demo"})
    probes.extend([
        {"system": "poc", "caller": "alpha-reader", "case": "reader_search", "http": 200,
         "exposed": "BIOSEAL-9151" in json.dumps(reader, ensure_ascii=False)},
        {"system": "poc", "caller": "alpha-reader", "case": "reader_citation",
         "http": citation.status_code, "exposed": citation.is_success},
    ])
    return probes


def run(poc: httpx.Client, ragflow: RAGFlow, repeats: int) -> dict:
    questions = (ROOT / "questions.json").read_bytes()
    samples = json.loads(questions)
    _, poc_corpus = seed(poc)
    datasets, documents = ragflow.seed()
    if any(row["sha256"] != poc_corpus[f"{tenant}/{filename}"]["sha256"]
           for tenant, files in documents.items() for filename, row in files.items()):
        raise RuntimeError("The two systems indexed different source files")
    coverage = indexed_evidence(ragflow, datasets, documents, samples)
    doc_locations = {
        row["id"]: (tenant, filename)
        for tenant, tenant_documents in documents.items() for filename, row in tenant_documents.items()
    }

    def measure(sample: dict, mode: str) -> dict:
        role = sample["token"]
        tenant = role.split("-")[0]
        start = perf_counter()
        if mode == "ragflow_native":
            config = ragflow.config[tenant]
            scope = datasets[tenant]["id"]
            keys = {row["id"] for row in documents[tenant].values()}
            if role == "alpha-reader":
                keys.remove(documents["alpha"]["audit.md"]["id"])
            key = config.get("auditor_key", config["key"]) if role == "alpha-auditor" else config["key"]
            response = ragflow.call(key, [scope], sample["question"], document_ids=sorted(keys))
            elapsed = (perf_counter() - start) * 1000
            native = data(response)
            chunks = []
            for chunk in native["chunks"] or []:
                doc_id = chunk["document_id"]
                if doc_id not in keys or chunk["dataset_id"] != scope:
                    raise RuntimeError(f'RAGFlow returned an out-of-scope document for {sample["id"]}')
                chunks.append({"filename": doc_locations[doc_id][1], "body": chunk["content"],
                               "chunk_id": chunk["id"], "document_id": doc_id,
                               **{k: chunk.get(k) for k in ("similarity", "term_similarity", "vector_similarity")}})
            stages = {}
        else:
            result = search(poc, role, sample["question"], mode.removeprefix("poc_"))
            elapsed = (perf_counter() - start) * 1000
            if result["degraded"]:
                raise RuntimeError(f"Degraded PoC request: {sample['id']} {mode}")
            chunks = result["results"]
            stages = result["debug"]["stage_ms"]
        return {
            "mode": mode, "id": sample["id"], "kind": sample["kind"], "http_ms": elapsed,
            "metrics": evidence_metrics(chunks, sample["gold"]), "ranked_evidence": chunks, "stage_ms": stages,
        }

    for sample in (samples[0], samples[-1], next(row for row in samples if row["token"] == "alpha-auditor")):
        for mode in MODES:
            measure(sample, mode)
    rows = []
    rng = random.Random(42)
    for trial in range(1, repeats + 1):
        print(f"Measured pass {trial}/{repeats}", flush=True)
        jobs = [(sample, mode) for sample in samples for mode in MODES]
        rng.shuffle(jobs)
        for sample, mode in jobs:
            rows.append({"trial": trial, **measure(sample, mode)})

    probes = isolation_probes(ragflow, poc, datasets, documents)
    return {
        "settings": {
            "questions_sha256": hashlib.sha256(questions).hexdigest(), "concurrency": 1, "repeats": repeats,
            "poc": {"search": TUNING, "health": poc.get("/health").raise_for_status().json(), "corpus": poc_corpus},
            "ragflow": {"version": "v1.0.0", "search": NATIVE_SETTINGS, "model": MODEL_ID,
                        "datasets": datasets, "documents": documents, "indexed_gold": coverage},
        },
        "samples": samples,
        "overall": {mode: aggregate([row for row in rows if row["mode"] == mode]) for mode in MODES},
        "by_kind": {kind: {
            mode: aggregate([row for row in rows if row["mode"] == mode and row["kind"] == kind])
            for mode in MODES
        } for kind in sorted({row["kind"] for row in samples})},
        "isolation": probes, "requests": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poc-url", default=BASE_URL)
    parser.add_argument("--ragflow-url", default=RF_URL)
    parser.add_argument("--credentials", type=Path, default=Path(".poc/ragflow-credentials.json"))
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path(".poc/ragflow-results.json"))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    credentials = json.loads(args.credentials.read_text())
    with (
        httpx.Client(base_url=args.poc_url, timeout=60, trust_env=False) as poc,
        httpx.Client(base_url=args.ragflow_url, timeout=60, trust_env=False) as native,
    ):
        report = run(poc, RAGFlow(native, credentials), args.repeat)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    for mode, result in report["overall"].items():
        print(mode, *(f"{key}={result[key]:.3f}" for key in ("recall@3", "recall@5", "p50_ms", "p95_ms")))
    print(args.out.resolve())


if __name__ == "__main__":
    main()
