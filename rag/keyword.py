import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx

INDEX_NAME = "rag-chunks-v1"
ES_URL = "http://127.0.0.1:59201"
BATCH_SIZE = 200
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,}(?:[-_][A-Za-z]+)*[-_]?\d+[A-Za-z0-9]*(?![A-Za-z0-9])")


@dataclass(frozen=True)
class IndexChunk:
    id: UUID
    filename: str
    heading: str | None
    body: str


class KeywordIndexError(Exception):
    pass


def identifiers(text: str) -> list[str]:
    return sorted({match.upper() for match in IDENTIFIER.findall(text)})


def scope_filters(tenant: UUID, library: UUID) -> list[dict]:
    return [
        {"term": {"tenant_id": str(tenant)}},
        {"term": {"knowledge_base_id": str(library)}},
    ]


def response_json(response: httpx.Response) -> dict:
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        raise KeywordIndexError("Invalid Elasticsearch response") from None
    if not isinstance(data, dict):
        raise KeywordIndexError("Invalid Elasticsearch response")
    return data


class KeywordIndex:
    def __init__(self, client: httpx.Client, index: str = INDEX_NAME):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,254}", index):
            raise ValueError("RAG_ES_INDEX must be a lowercase Elasticsearch index name")
        self.client = client
        self.path = f"/{index}"

    @classmethod
    @contextmanager
    def from_env(cls) -> Iterator["KeywordIndex"]:
        token = os.getenv("RAG_ES_API_KEY")
        headers = {"Authorization": f"ApiKey {token}"} if token else {}
        with httpx.Client(
            base_url=os.getenv("RAG_ES_URL", ES_URL), headers=headers, timeout=10, trust_env=False,
        ) as client:
            yield cls(client, os.getenv("RAG_ES_INDEX", INDEX_NAME))

    def initialize(self) -> None:
        fields = {
            name: {"type": "keyword"} for name in (
                "tenant_id", "knowledge_base_id", "document_id", "version_id", "chunk_id", "readers", "identifiers",
            )
        }
        fields["unrestricted"] = {"type": "boolean"}
        fields["revision"] = {"type": "long"}
        fields.update({name: {"type": "text", "analyzer": "cjk"} for name in ("heading", "body", "filename")})
        response = self.client.put(self.path, json={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {"dynamic": "strict", "properties": fields},
        })
        if response.status_code == 400:
            try:
                if response.json()["error"]["type"] == "resource_already_exists_exception":
                    return
            except (KeyError, TypeError, ValueError):
                pass
        data = response_json(response)
        if data.get("acknowledged") is not True or data.get("shards_acknowledged") is not True:
            raise KeywordIndexError("Elasticsearch index creation was incomplete")

    def replace_document(
        self, tenant: UUID, library: UUID, document: UUID, version: UUID | None, revision: int,
        readers: list[UUID] | None, chunks: list[IndexChunk],
    ) -> None:
        self.initialize()
        metadata = {
            "tenant_id": str(tenant), "knowledge_base_id": str(library), "document_id": str(document),
            "version_id": str(version) if version else None, "revision": revision,
            "unrestricted": readers is None, "readers": [str(reader) for reader in readers or []],
        }
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start:start + BATCH_SIZE]
            lines = []
            for chunk in batch:
                chunk_id = str(chunk.id)
                lines.append(json.dumps({
                    "index": {"_id": chunk_id, "version": revision, "version_type": "external_gte"},
                }))
                lines.append(json.dumps({
                    **metadata, "chunk_id": chunk_id, "filename": chunk.filename, "heading": chunk.heading,
                    "body": chunk.body, "identifiers": identifiers(f"{chunk.filename} {chunk.heading or ''} {chunk.body}"),
                }, ensure_ascii=False))
            response = self.client.post(
                f"{self.path}/_bulk", content=("\n".join(lines) + "\n").encode(),
                headers={"Content-Type": "application/x-ndjson"},
            )
            data = response_json(response)
            items = data.get("items")
            if data.get("errors") is not False or not isinstance(items, list) or len(items) != len(batch):
                raise KeywordIndexError("Elasticsearch did not index the entire batch")
            try:
                complete = all(
                    item["index"]["status"] in (200, 201) and item["index"]["_shards"]["failed"] == 0
                    for item in items
                )
            except (KeyError, TypeError):
                complete = False
            if not complete:
                raise KeywordIndexError("Elasticsearch did not index the entire batch")

        # Make inserts searchable before the cleanup takes its snapshot, including any partial prior attempt.
        refreshed = response_json(self.client.post(f"{self.path}/_refresh"))
        if refreshed.get("_shards", {}).get("failed") != 0:
            raise KeywordIndexError("Elasticsearch refresh was incomplete")
        filters = scope_filters(tenant, library) + [
            {"term": {"document_id": str(document)}},
            {"range": {"revision": {"lt" if version else "lte": revision}}},
        ]
        deleted = response_json(self.client.post(
            f"{self.path}/_delete_by_query",
            params={"refresh": "true"},
            json={"query": {"bool": {"filter": filters}}},
        ))
        if deleted.get("timed_out") is not False or deleted.get("failures") or deleted.get("version_conflicts"):
            raise KeywordIndexError("Elasticsearch did not remove all stale chunks")

    def search(
        self, tenant: UUID, library: UUID, reader: UUID, question: str, limit: int = 30,
    ) -> list[UUID]:
        """Return candidate IDs. The caller checks current versions and permissions in PostgreSQL."""
        filters = scope_filters(tenant, library) + [{"bool": {
            "should": [{"term": {"unrestricted": True}}, {"term": {"readers": str(reader)}}],
            "minimum_should_match": 1,
        }}]
        matches = [
            {"multi_match": {"query": question, "fields": ["body", "heading^2", "filename"]}},
            {"match_phrase": {"body": {"query": question, "boost": 2}}},
        ]
        terms = identifiers(question)
        if terms:
            matches.append({"terms": {"identifiers": terms, "boost": 4}})
        response = self.client.post(f"{self.path}/_search", json={
            "size": limit, "_source": False,
            "query": {"bool": {"filter": filters, "should": matches, "minimum_should_match": 1}},
        })
        data = response_json(response)
        if data.get("timed_out") or data.get("_shards", {}).get("failed"):
            raise KeywordIndexError("Elasticsearch search was incomplete")
        try:
            return [UUID(hit["_id"]) for hit in data["hits"]["hits"]]
        except (KeyError, TypeError, ValueError):
            raise KeywordIndexError("Elasticsearch returned invalid candidate IDs") from None
