"""Evaluate the published policy across formats through an isolated RAG service."""

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from time import monotonic, sleep
from typing import Literal

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .dev import ROOT, available_port, command, new_run, services, store_json
from .locations import Location
from .models import EMBEDDING_MODEL, RERANK_MODEL

DATASET = Path(__file__).with_name("policy_eval_cases.json")
FORMATS = ("markdown", "docx", "mixed-pdf")
CONFLICTS = ("plain-pdf", "shift-service", "shift-ops")
PHASE_SOURCES = {**{name: (name,) for name in FORMATS}, "conflicts": CONFLICTS,
                 "revoked-pending": ("mixed-pdf",), "revoked-synced": ("mixed-pdf",)}
LOCATOR = TypeAdapter(Location)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: Location
    quote: str = Field(min_length=1)
    parse_report: dict | None
    omitted_text: str | None = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    phase: str
    question: str
    as_of: date | None = None
    status: Literal["answered", "insufficient", "policy_conflict"]
    reason: str | None = None
    gold: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    candidate_gold: list[str] = Field(default_factory=list)
    conflict_gold: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    fact_any: list[str] = Field(default_factory=list)


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: dict[str, str]
    sources: dict[str, Source]
    cases: list[Case]


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_suite(raw: bytes) -> tuple[Suite, dict[str, bytes]]:
    suite = Suite.model_validate_json(raw)
    if set(suite.sources) != set(FORMATS + CONFLICTS):
        raise ValueError("Policy evaluation needs the format and conflict fixtures")
    if {case.phase for case in suite.cases} != set(PHASE_SOURCES) or len(suite.cases) != len({c.id for c in suite.cases}):
        raise ValueError("Policy cases must cover all phases and use unique IDs")
    for case in suite.cases:
        targets = set(case.gold + case.excluded + case.candidate_gold + case.conflict_gold + case.blocked)
        if case.question not in suite.questions or targets - set(PHASE_SOURCES[case.phase]):
            raise ValueError(f"Invalid question or gold source for {case.id}")
    originals = {name: (ROOT / source.path).read_bytes() for name, source in suite.sources.items()}
    for name, content in originals.items():
        if sha256(content) != suite.sources[name].sha256:
            raise ValueError(f"Original SHA-256 differs from gold for {name}")
    return suite, originals


def provision(env: dict, log) -> tuple[dict, dict, str]:
    def cli(*args):
        return json.loads(command(sys.executable, "-m", "rag.cli", *args, env=env,
                                  stdout=subprocess.PIPE, stderr=log).stdout)

    tenant = "policy-eval"
    cli("create-tenant", tenant, "Policy evaluation")
    writer = cli("register-app", tenant, "Writer")
    reader = cli("register-app", tenant, "Reader")
    for library in ("returns", "conflicts"):
        cli("create-library", tenant, library, library)
        cli("grant", tenant, writer["id"], library, "write")
        cli("grant", tenant, reader["id"], library, "read")
    return {"Authorization": f"Bearer {writer['key']}"}, {"Authorization": f"Bearer {reader['key']}"}, reader["id"]


def chunk_location(location: dict) -> dict:
    if "source" in location:
        return location["source"]
    return {"kind": "lines", "line_start": location.get("line_start"), "line_end": location.get("line_end")}


def check_parse(conn, source: Source, receipt: dict, state: dict) -> dict:
    version_id = receipt["version_id"]
    content, checksum = conn.execute("SELECT content, checksum FROM rag.versions WHERE id=%s", (version_id,)).fetchone()
    rows = conn.execute("SELECT id::text, body, location FROM rag.chunks WHERE version=%s ORDER BY ordinal",
                        (version_id,)).fetchall()
    location = LOCATOR.dump_python(source.location, mode="json")
    anchors = [chunk for chunk, text, loc in rows if chunk_location(loc) == location and source.quote in text]
    report = state["parse_report"]
    checks = {
        "published": state["state"] == "done" and state["published_version_id"] == version_id,
        "original_sha256": sha256(content) == checksum.hex() == source.sha256,
        "quote_at_location": bool(anchors),
        "parse_report": (report is None if source.parse_report is None else report is not None
                         and all(report.get(key) == value for key, value in source.parse_report.items())),
        "omitted_text_absent": source.omitted_text is None or all(source.omitted_text not in text for _, text, _ in rows),
    }
    return {"passed": all(checks.values()), "checks": checks, "job": state, "anchor_chunks": anchors,
            "sha256": sha256(content), "chunk_count": len(rows)}


def evidence_source(item: dict, suite: Suite, parsed: dict) -> str | None:
    """Match the current run's receipt to the immutable original location and quote."""
    for name, record in parsed.items():
        job = record["job"]
        source = suite.sources[name]
        if (item.get("version_id") == job["version_id"] and item.get("document_id") == job["document_id"]
                and item.get("location") == LOCATOR.dump_python(source.location, mode="json")
                and source.quote in item.get("body", "")):
            return name
    return None


def origin_path(job: dict) -> str:
    return f"/v1/documents/{job['document_id']}/file?version_id={job['version_id']}"


def score_case(client, reader, case: Case, library: str, suite: Suite, parsed: dict, sync_state: dict | None) -> dict:
    question = suite.questions[case.question]
    day = case.as_of.isoformat() if case.as_of else None
    query = {"knowledge_base_code": library, "question": question, "as_of": day}

    def get(path):
        return client.get(path, headers=reader)

    def post(path, payload):
        response = client.post(path, headers=reader, json=query | payload)
        return response.status_code, response.json() if response.is_success else {}

    def describe(item):
        return {"source": evidence_source(item, suite, parsed), "chunk_id": item["chunk_id"],
                "version_id": item["version_id"], "location": item.get("location")}

    visible_versions = {parsed[name]["job"]["version_id"] for name in PHASE_SOURCES[case.phase]}
    modes = {}
    for mode in ("keyword", "vector"):
        code, response = post("/v1/retrieve", {"mode": mode, "interpret": True, "debug": True})
        results = [describe(item) for item in response.get("results", [])]
        conflicts = [describe(item) for item in response.get("policy_conflicts", [])]
        candidates = response.get("debug", {}).get("recall", {}).get(mode, [])
        ranks = {item["source"]: rank for rank, item in reversed(list(enumerate(results, 1))) if item["source"]}
        candidate_sources = [name for name in PHASE_SOURCES[case.phase] if set(candidates) & set(parsed[name]["anchor_chunks"])]
        checks = {
            "http": code == 200 and response.get("mode") == mode and response.get("degraded") == [],
            "visible_versions": all(item["version_id"] in visible_versions for item in results + conflicts),
            "gold": set(case.gold) <= ranks.keys(),
            "excluded": not set(case.excluded) & ranks.keys(),
            "conflict_gold": {item["source"] for item in conflicts} == set(case.conflict_gold),
            "candidate_gold": mode != "vector" or set(case.candidate_gold) <= set(candidate_sources),
            "blocked": not case.blocked or not (results or conflicts or candidates),
        }
        modes[mode] = {"passed": all(checks.values()), "checks": checks, "http": code,
                       "ranks": ranks, "candidate_sources": candidate_sources, "candidates": candidates,
                       "results": results, "conflicts": conflicts, "degraded": response.get("degraded")}

    code, answer = post("/v1/answer", {"strategy": "fixed"})
    claims = answer.get("claims", [])
    citations = answer.get("citations", [])
    bound_ids = {identifier for claim in claims for identifier in claim["evidence_ids"]}
    texts = [claim["text"] for claim in claims]
    answer_checks = {
        "status": code == 200 and answer.get("status") == case.status,
        "reason": case.reason is None or answer.get("reason") == case.reason,
        "effective_date": day is None or answer.get("effective_date") == day,
        "facts": not claims if case.status != "answered" else bool(claims)
        and (not case.fact_any or any(term in text for term in case.fact_any for text in texts)),
        "claim_bindings": case.status != "answered" or bound_ids == {entry["chunk_id"] for entry in citations},
    }

    readbacks = []
    for citation in citations:
        path = f"/v1/citations/{citation['chunk_id']}"
        response = get(path)
        evidence = response.json() if response.is_success else {}
        source = evidence_source(evidence, suite, parsed)
        original = get(origin_path(parsed[source]["job"])) if source else None
        digest = sha256(original.content) if original is not None and original.is_success else None
        passed = (source is not None and digest == suite.sources[source].sha256 and citation["href"] == path
                  and all(citation.get(key) == evidence.get(key)
                          for key in ("chunk_id", "document_id", "version_id", "location")))
        readbacks.append({"source": source, "citation": citation, "citation_http": response.status_code,
                          "original_http": original.status_code if original is not None else None,
                          "original_sha256": digest, "passed": passed})

    blocked = {}
    for name in case.blocked:
        record = parsed[name]
        blocked[name] = {"citations": [get(f"/v1/citations/{chunk}").status_code for chunk in record["anchor_chunks"]],
                         "original": get(origin_path(record["job"])).status_code}
    deny_ok = all(item["citations"] and all(status == 404 for status in item["citations"]) and item["original"] == 404
                  for item in blocked.values())
    citation_ok = (code == 200 and len(readbacks) == len(case.gold) and all(item["passed"] for item in readbacks)
                   and {item["source"] for item in readbacks} == set(case.gold) and deny_ok)
    stages = {
        "parse": {"passed": all(parsed[name]["passed"] for name in PHASE_SOURCES[case.phase]),
                  "sources": list(PHASE_SOURCES[case.phase])},
        "retrieval": {"passed": all(result["passed"] for result in modes.values())
                      and (sync_state is None or sync_state["passed"]), "modes": modes, "sync_state": sync_state},
        "citation": {"passed": citation_ok, "readbacks": readbacks, "blocked": blocked},
        "answer": {"passed": all(answer_checks.values()), "checks": answer_checks, "http": code,
                   "status": answer.get("status"), "reason": answer.get("reason"), "claims": claims,
                   "effective_date": answer.get("effective_date"), "model": answer.get("model"),
                   "model_calls": answer.get("model_calls")},
    }
    return {"case": case.model_dump(mode="json"), "question": question, "stages": stages,
            "passed": all(stage["passed"] for stage in stages.values())}


def evaluate(env, folder, manifest, suite, originals, report):
    env.update(RAG_INGEST_EXECUTOR="pg", RAG_SYNC_EXECUTOR="pg", RAG_ES_REQUIRE_ALIAS="0")
    log_path = folder / "services.log"
    with services(env, available_port(), log_path, sync=False) as (url, _), log_path.open("ab") as log, \
         httpx.Client(base_url=url, timeout=25, trust_env=False) as client, \
         psycopg.connect(env["RAG_TEST_OWNER_URL"], autocommit=True) as db:
        writer, reader, reader_id = provision(env, log)
        parsed = report["sources"]

        def save():
            store_json(folder / "policy-eval.json", report)

        def job_status(job):
            return client.get(f"/v1/ingestion-jobs/{job['job_id']}", headers=writer).raise_for_status().json()

        def sync_once():
            command(sys.executable, "-m", "rag.sync", "--once", env=env, stdout=log, stderr=log)

        def add(name, library, document_id=None):
            source = suite.sources[name]
            params = {"filename": Path(source.path).name}
            if document_id:
                params["document_id"] = document_id
            job = client.post(f"/v1/knowledge-bases/{library}/documents", params=params, content=originals[name],
                              headers=writer | {"Idempotency-Key": name}).raise_for_status().json()
            deadline = monotonic() + 45
            while (state := job_status(job))["state"] not in ("done", "failed") and monotonic() < deadline:
                sleep(0.1)
            parsed[name] = check_parse(db, source, job, state)
            if state["state"] == "done" and state["published_version_id"] == job["version_id"]:
                sync_once()
                parsed[name]["job"] = job_status(job)
            save()
            return job

        def phase(name, library, sync_state=None):
            for case in suite.cases:
                if case.phase == name:
                    row = score_case(client, reader, case, library, suite, parsed, sync_state)
                    report["cases"].append(row)
                    save()
                    statuses = " ".join(f"{key}={'ok' if value['passed'] else 'FAIL'}" for key, value in row["stages"].items())
                    print(f"{case.id} {case.as_of or 'default'} {statuses} {row['stages']['answer']['status']}", flush=True)

        document = None
        for name in FORMATS:
            job = add(name, "returns", document)
            document = job["document_id"]
            phase(name, "returns")
        for name in CONFLICTS:
            add(name, "conflicts")
        phase("conflicts", "conflicts")

        client.patch(f"/v1/documents/{document}/readers", headers=writer, json={"readers": []}).raise_for_status()
        es_headers = {"Authorization": f"ApiKey {env['RAG_ES_API_KEY']}"} if env.get("RAG_ES_API_KEY") else {}
        reader_filter = {"bool": {"should": [{"term": {"unrestricted": True}}, {"term": {"readers": reader_id}}],
                                 "minimum_should_match": 1}}
        index_query = {"size": 0, "track_total_hits": True, "query": {"term": {"version_id": job["version_id"]}},
                       "aggs": {"readable": {"filter": reader_filter}}}
        for name, expected in (("revoked-pending", "pending"), ("revoked-synced", "synced")):
            if expected == "synced":
                sync_once()
            status = job_status(job)["keyword_status"]
            indexed = httpx.post(f"{env['RAG_ES_URL']}/{manifest['index']}/_search", headers=es_headers,
                                 json=index_query, timeout=10, trust_env=False).raise_for_status().json()
            count = indexed["hits"]["total"]["value"]
            readable = indexed["aggregations"]["readable"]["doc_count"]
            expected_readable = count if expected == "pending" else 0
            phase(name, "returns", {
                "expected": expected, "status": status, "es_chunks": count, "es_readable_chunks": readable,
                "passed": status == expected and not indexed["timed_out"] and indexed["_shards"]["failed"] == 0
                and count == parsed["mixed-pdf"]["chunk_count"] > 0 and readable == expected_readable,
            })


def run() -> None:
    raw = DATASET.read_bytes()
    suite, originals = load_suite(raw)
    with new_run("policy-eval") as (env, folder, manifest):
        report = {"dataset_sha256": sha256(raw), "settings": {"embedding": EMBEDDING_MODEL, "rerank": RERANK_MODEL,
                  "strategy": "fixed", "modes": ["keyword", "vector"]},
                  "gold_sources": {name: source.model_dump(mode="json") for name, source in suite.sources.items()},
                  "sources": {}, "cases": []}
        evaluate(env, folder, manifest, suite, originals, report)
        rows = report["cases"]
        passed = sum(row["passed"] for row in rows)
        report["summary"] = {"passed": passed, "total": len(rows), "by_stage": {
            stage: sum(row["stages"][stage]["passed"] for row in rows) for stage in ("parse", "retrieval", "citation", "answer")
        }}
        out = folder / "policy-eval.json"
        store_json(out, report)
        print(f"{passed}/{len(rows)} cases passed. Report: {out}", flush=True)
        if passed != len(rows):
            raise SystemExit(1)
