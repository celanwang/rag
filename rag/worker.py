import argparse
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid5

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .db import SCHEMA_OPTIONS, set_tenant
from .models import EMBEDDING_BATCH_SIZE, BailianModels, ModelError
from .objects import ObjectStore, StorageError
from .text import CONFIG, MAX_UPLOAD_BYTES, Chunk, InvalidText, decode_text, split_text

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 4
LEASE_SECONDS = 120


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    job: UUID
    tenant: UUID
    token: UUID


@dataclass(frozen=True)
class Source:
    library: UUID
    document: UUID
    version: UUID
    mime: str
    path: str
    checksum: bytes
    config: dict


@contextmanager
def tenant_transaction(conn: psycopg.Connection, lease: Lease):
    with conn.transaction():
        set_tenant(conn, lease.tenant)
        yield


def claim(conn: psycopg.Connection) -> Lease | None:
    row = conn.execute(
        "SELECT id, tenant, lease FROM rag.claim_ingest(%s, %s)", (MAX_ATTEMPTS, LEASE_SECONDS),
    ).fetchone()
    return Lease(*row) if row and row[2] else None


def read_source(conn: psycopg.Connection, lease: Lease) -> Source:
    with tenant_transaction(conn, lease):
        row = conn.execute(
            """
            SELECT v.library, v.document, v.id, v.mime, v.path, v.checksum, v.config
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


def fail_in_transaction(conn: psycopg.Connection, lease: Lease, error: str, retryable: bool) -> str:
    row = conn.execute(
        """
        UPDATE jobs
        SET state = CASE WHEN %s AND attempts < %s THEN 'retry' ELSE 'failed' END,
            available = clock_timestamp() + power(2, attempts) * interval '1 second',
            lease = NULL, deadline = NULL, error = %s
        WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
        RETURNING state
        """,
        (retryable, MAX_ATTEMPTS, error, lease.job, lease.token),
    ).fetchone()
    if row is None:
        raise LeaseLost
    return row[0]


def publish(
    conn: psycopg.Connection, lease: Lease, source: Source,
    chunks: list[Chunk], vectors: list[list[float]],
) -> bool:
    with tenant_transaction(conn, lease):
        document = conn.execute(
            "SELECT target, deleted FROM documents WHERE id = %s AND library = %s FOR UPDATE",
            (source.document, source.library),
        ).fetchone()
        owned = conn.execute(
            """
            SELECT id FROM jobs
            WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
            FOR UPDATE
            """,
            (lease.job, lease.token),
        ).fetchone()
        if owned is None:
            raise LeaseLost
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
            INSERT INTO jobs (tenant, library, document, kind, revision, stage)
            VALUES (%s, %s, %s, 'sync', %s, 'accepted')
            """,
            (lease.tenant, source.library, source.document, revision),
        )
        done = conn.execute(
            """
            UPDATE jobs SET state = 'done', stage = 'published', lease = NULL, deadline = NULL, error = NULL
            WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
            RETURNING id
            """,
            (lease.job, lease.token),
        ).fetchone()
        if done is None:
            raise LeaseLost
        return True


def process(conn: psycopg.Connection, lease: Lease, objects: ObjectStore, models: BailianModels) -> None:
    try:
        source = read_source(conn, lease)
        if source.config != CONFIG:
            raise InvalidText("Unsupported version configuration")
        original = objects.get(source.path, MAX_UPLOAD_BYTES)
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
        logger.info(
            "Ingest %s job=%s tenant=%s document=%s version=%s",
            "published" if published else "superseded", lease.job, lease.tenant, source.document, source.version,
        )
        return
    except LeaseLost:
        return
    except StorageError as error:
        reason, retryable = "original_unavailable", error.retryable
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
        logger.warning("Ingest %s job=%s tenant=%s error=%s", state, lease.job, lease.tenant, reason)
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
    objects = ObjectStore.from_env()
    try:
        with BailianModels.from_env() as models:
            while True:
                try:
                    with psycopg.connect(dsn, autocommit=True, connect_timeout=5, options=SCHEMA_OPTIONS) as conn:
                        while True:
                            lease = claim(conn)
                            if lease:
                                process(conn, lease, objects, models)
                            if args.once:
                                return
                            if not lease:
                                time.sleep(1)
                except psycopg.Error as error:
                    if args.once:
                        raise SystemExit(f"Worker database operation failed ({type(error).__name__}).") from None
                    logger.error("Worker database operation failed (%s)", type(error).__name__)
                    time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        objects.close()


if __name__ == "__main__":
    main()
