import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from functools import partial
from uuid import UUID, uuid5

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .audit import emit
from .jobs import (
    LEASE_SECONDS, Lease, LeaseLost, complete, fail_in_transaction, lock_lease, run_worker, tenant_transaction,
)
from .models import EMBEDDING_BATCH_SIZE, BailianModels, ModelError
from .text import CONFIG, MAX_UPLOAD_BYTES, Chunk, InvalidText, decode_text, split_text

@dataclass(frozen=True)
class Source:
    library: UUID
    document: UUID
    version: UUID
    mime: str
    content: bytes
    checksum: bytes
    config: dict
    actor: UUID
    trace: UUID


def read_source(conn: psycopg.Connection, lease: Lease) -> Source:
    with tenant_transaction(conn, lease):
        row = conn.execute(
            """
            SELECT v.library, v.document, v.id, v.mime, v.content, v.checksum, v.config, j.app, COALESCE(j.trace, j.id)
            FROM jobs AS j
            JOIN versions AS v
              ON (v.tenant, v.library, v.document, v.id) = (j.tenant, j.library, j.document, j.version)
            WHERE j.id = %s AND j.lease = %s AND j.deadline > clock_timestamp()
            """,
            (lease.job, lease.token),
        ).fetchone()
        if row is None:
            raise LeaseLost
        return Source(*row)


def advance(conn: psycopg.Connection, lease: Lease, stage: str) -> None:
    with tenant_transaction(conn, lease):
        row = conn.execute(
            """
            UPDATE jobs SET stage = %s, deadline = clock_timestamp() + %s * interval '1 second'
            WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
            RETURNING id
            """,
            (stage, LEASE_SECONDS, lease.job, lease.token),
        ).fetchone()
        if row is None:
            raise LeaseLost


def publish(
    conn: psycopg.Connection, lease: Lease, source: Source,
    chunks: list[Chunk], vectors: list[list[float]],
) -> bool:
    with tenant_transaction(conn, lease):
        document = conn.execute(
            "SELECT target, deleted FROM documents WHERE id = %s AND library = %s FOR UPDATE",
            (source.document, source.library),
        ).fetchone()
        lock_lease(conn, lease)
        if not document or document[0] != source.version or document[1] is not None:
            fail_in_transaction(conn, lease, "superseded", retryable=False)
            return False

        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO chunks (id, tenant, library, document, version, ordinal, heading, body, location, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::public.vector)
                """,
                (
                    (
                        uuid5(source.version, str(ordinal)), lease.tenant, source.library, source.document,
                        source.version, ordinal, chunk.heading, chunk.body,
                        Jsonb({"line_start": chunk.line_start, "line_end": chunk.line_end}),
                        json.dumps(vector),
                    )
                    for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ),
            )
        revision = conn.execute(
            "UPDATE documents SET published = %s, revision = revision + 1 WHERE id = %s RETURNING revision",
            (source.version, source.document),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO jobs (tenant, library, document, kind, revision, stage, app, trace)
            VALUES (%s, %s, %s, 'sync', %s, 'accepted', %s, %s)
            """,
            (lease.tenant, source.library, source.document, revision, source.actor, source.trace),
        )
        complete(conn, lease, "published")
        return True


def process(conn: psycopg.Connection, lease: Lease, models: BailianModels) -> None:
    try:
        source = read_source(conn, lease)
        if source.config != CONFIG:
            raise InvalidText("Unsupported version configuration")
        original = source.content
        if len(original) > MAX_UPLOAD_BYTES or hashlib.sha256(original).digest() != source.checksum:
            raise InvalidText("Original checksum does not match")
        chunks = split_text(decode_text(original), markdown=source.mime == "text/markdown")
        if not chunks:
            raise InvalidText("Document contains no indexable text")

        vectors = []
        for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            advance(conn, lease, "embed")
            batch = chunks[start:start + EMBEDDING_BATCH_SIZE]
            vectors.extend(models.embed([chunk.passage() for chunk in batch]))
        advance(conn, lease, "publish")
        published = publish(conn, lease, source, chunks, vectors)
        emit("document.publish", source.trace, outcome="published" if published else "superseded",
             actor_type="app", actor=source.actor, service="ingest", tenant=lease.tenant,
             job=lease.job, library=source.library, document=source.document, version=source.version)
        return
    except LeaseLost:
        return
    except InvalidText:
        reason, retryable = "content_invalid", False
    except httpx.RequestError:
        reason, retryable = "embedding_unavailable", True
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        retryable = status in {408, 429} or status >= 500
        reason = "embedding_unavailable" if retryable else "embedding_rejected"
    except ModelError:
        reason, retryable = "embedding_invalid_response", True

    try:
        with tenant_transaction(conn, lease):
            state = fail_in_transaction(conn, lease, reason, retryable)
        emit("document.publish", source.trace, outcome=state, actor_type="app", actor=source.actor,
             service="ingest", tenant=lease.tenant, job=lease.job, library=source.library,
             document=source.document, version=source.version, error=reason)
    except LeaseLost:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued RAG document ingestion tasks.")
    parser.add_argument("--once", action="store_true", help="Claim at most one available task and exit")
    args = parser.parse_args()
    dsn = os.getenv("RAG_WORKER_DATABASE_URL")
    if not dsn:
        parser.error("set RAG_WORKER_DATABASE_URL before running the worker")

    logging.basicConfig(level=logging.INFO)
    # Worker logs describe job outcomes without request payloads.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        with BailianModels.from_env() as models:
            run_worker(dsn, "ingest", partial(process, models=models), once=args.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
