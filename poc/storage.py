import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .documents import Chunk, identifiers

DSN = os.getenv("RAG_POSTGRES_DSN", "postgresql://rag_app:poc-app@127.0.0.1:55432/rag")
ES_URL = os.getenv("RAG_ES_URL", "http://127.0.0.1:59200")
ES_INDEX = "rag-poc-chunks-v1"

PUBLISHED_CHUNKS = """
    FROM chunks c
    JOIN documents d USING (tenant_id, knowledge_base_id, document_id)
    WHERE c.tenant_id = %s AND c.knowledge_base_id = %s
      AND c.version_id = d.published_version
      AND (cardinality(d.readers) = 0 OR %s = ANY(d.readers))
"""


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    knowledge_base_id: str
    subject: str

    @property
    def key(self) -> tuple[str, str]:
        return self.tenant_id, self.knowledge_base_id

    def document_id(self, filename: str) -> UUID:
        return uuid5(NAMESPACE_URL, json.dumps([*self.key, filename], ensure_ascii=False))


class Postgres:
    def __init__(self):
        self.pool = ConnectionPool(
            DSN, min_size=1, max_size=8, timeout=3, open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "options": "-c statement_timeout=3000"},
        )

    @contextmanager
    def session(self, scope: Scope):
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("SELECT set_config('rag.tenant_id', %s, true)", (scope.tenant_id,))
            yield connection

    @staticmethod
    def lock_document(connection, document_id: UUID) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(document_id),))

    def publish(
        self, scope: Scope, filename: str, fingerprint: str, readers: list[str],
        chunks: list[Chunk], embeddings: list[list[float]],
    ) -> UUID:
        document_id = scope.document_id(filename)
        with self.session(scope) as connection:
            self.lock_document(connection, document_id)
            current = connection.execute("""
                SELECT fingerprint, published_version FROM documents
                WHERE tenant_id=%s AND knowledge_base_id=%s AND document_id=%s
            """, (*scope.key, document_id)).fetchone()
            unchanged = current is not None and current["fingerprint"] == fingerprint
            previous_version = current["published_version"] if current else document_id
            version = previous_version if unchanged else uuid5(previous_version, fingerprint)
            connection.execute("""
                INSERT INTO documents VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, knowledge_base_id, document_id) DO UPDATE SET
                    fingerprint = EXCLUDED.fingerprint, published_version = EXCLUDED.published_version,
                    readers = EXCLUDED.readers
            """, (*scope.key, document_id, filename, fingerprint, version, readers))
            if not unchanged:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                        INSERT INTO chunks VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    """, [
                        (*scope.key, document_id, version, uuid5(version, str(i)),
                         chunk.heading, chunk.body, chunk.line_start, chunk.line_end, json.dumps(vector))
                        for i, (chunk, vector) in enumerate(zip(chunks, embeddings, strict=True))
                    ])
        return version

    def sync(self, scope: Scope, document_id: UUID, keyword: "Elasticsearch") -> UUID:
        with self.session(scope) as connection:
            # Share the publish lock so a delayed sync always indexes the current version and ACL.
            self.lock_document(connection, document_id)
            document = connection.execute("""
                SELECT filename, published_version, readers FROM documents
                WHERE tenant_id=%s AND knowledge_base_id=%s AND document_id=%s
            """, (*scope.key, document_id)).fetchone()
            version = document["published_version"]
            chunks = connection.execute("""
                SELECT chunk_id, heading, body FROM chunks
                WHERE tenant_id=%s AND knowledge_base_id=%s AND document_id=%s AND version_id=%s
            """, (*scope.key, document_id, version)).fetchall()
            keyword.sync(scope, document_id, version, document, chunks)
        return version

    def search(self, scope: Scope, embedding: list[float], limit: int) -> list[str]:
        with self.session(scope) as connection:
            rows = connection.execute(
                "SELECT c.chunk_id::text " + PUBLISHED_CHUNKS
                + " ORDER BY c.embedding <=> %s::vector, c.chunk_id LIMIT %s",
                (*scope.key, scope.subject, json.dumps(embedding), limit),
            ).fetchall()
        return [row["chunk_id"] for row in rows]

    def evidence(self, scope: Scope, candidates: set[str]) -> dict[str, dict]:
        with self.session(scope) as connection:
            rows = connection.execute("""
                SELECT c.chunk_id::text, c.document_id::text, c.version_id::text,
                       d.filename, c.heading, c.body, c.line_start, c.line_end
            """ + PUBLISHED_CHUNKS + " AND c.chunk_id = ANY(%s::uuid[])",
                (*scope.key, scope.subject, list(candidates)),
            ).fetchall()
        return {row["chunk_id"]: row for row in rows}

    def versions(self) -> dict[str, str]:
        with self.pool.connection() as connection:
            return connection.execute("""
                SELECT current_setting('server_version') AS postgres,
                       (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector
            """).fetchone()


class KeywordError(Exception):
    pass


class Elasticsearch:
    def __init__(self):
        self.client = httpx.Client(base_url=ES_URL, timeout=3, trust_env=False)

    def initialize(self) -> None:
        fields = {
            name: {"type": "keyword"} for name in (
                "tenant_id", "knowledge_base_id", "document_id", "version_id",
                "chunk_id", "readers", "identifiers",
            )
        }
        fields["public"] = {"type": "boolean"}
        fields.update({name: {"type": "text", "analyzer": "cjk"} for name in ("heading", "body", "filename")})
        response = self.client.put(f"/{ES_INDEX}", json={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {"dynamic": "strict", "properties": fields},
        })
        if response.status_code == 400 and response.json()["error"]["type"] == "resource_already_exists_exception":
            return
        response.raise_for_status()

    def sync(self, scope: Scope, document_id: UUID, version: UUID, document: dict, chunks: list[dict]) -> None:
        bulk = []
        for chunk in chunks:
            chunk_id = str(chunk["chunk_id"])
            source = {
                "tenant_id": scope.tenant_id, "knowledge_base_id": scope.knowledge_base_id,
                "document_id": str(document_id), "version_id": str(version), "chunk_id": chunk_id,
                "filename": document["filename"], "heading": chunk["heading"], "body": chunk["body"],
                "readers": document["readers"], "public": not document["readers"],
                "identifiers": identifiers(f'{document["filename"]} {chunk["heading"]} {chunk["body"]}'),
            }
            bulk.extend([
                {"update": {"_index": ES_INDEX, "_id": chunk_id}},
                {"doc_as_upsert": True, "doc": source},
            ])
        response = self.client.post("/_bulk", params={"refresh": "true"},
                                    content="".join(json.dumps(line) + "\n" for line in bulk),
                                    headers={"Content-Type": "application/x-ndjson"})
        response.raise_for_status()
        if response.json()["errors"]:
            raise KeywordError("Elasticsearch bulk indexing failed")

        response = self.client.post(f"/{ES_INDEX}/_delete_by_query", params={"refresh": "true"}, json={
            "query": {"bool": {
                "filter": [
                    {"term": {"tenant_id": scope.tenant_id}},
                    {"term": {"knowledge_base_id": scope.knowledge_base_id}},
                    {"term": {"document_id": str(document_id)}},
                ],
                "must_not": [{"term": {"version_id": str(version)}}],
            }},
        })
        response.raise_for_status()
        result = response.json()
        if result["timed_out"] or result["failures"]:
            raise KeywordError("Elasticsearch historical chunk cleanup failed")

    def search(self, scope: Scope, question: str, limit: int) -> list[str]:
        matches = [
            {"multi_match": {"query": question, "fields": ["heading^2", "body", "filename"]}},
            {"match_phrase": {"body": {"query": question, "boost": 2}}},
        ]
        if codes := identifiers(question):
            matches.append({"terms": {"identifiers": codes, "boost": 8}})
        response = self.client.post(f"/{ES_INDEX}/_search", json={
            "size": limit, "_source": False, "timeout": "2500ms",
            "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
            "query": {"bool": {
                "filter": [
                    {"term": {"tenant_id": scope.tenant_id}},
                    {"term": {"knowledge_base_id": scope.knowledge_base_id}},
                    {"bool": {"should": [
                        {"term": {"public": True}}, {"term": {"readers": scope.subject}},
                    ], "minimum_should_match": 1}},
                ],
                "should": matches, "minimum_should_match": 1,
            }},
        })
        response.raise_for_status()
        result = response.json()
        if result["timed_out"] or result["_shards"]["failed"]:
            raise KeywordError("Elasticsearch returned partial results")
        return [hit["_id"] for hit in result["hits"]["hits"]]

    def version(self) -> str:
        response = self.client.get("/")
        response.raise_for_status()
        return response.json()["version"]["number"]
