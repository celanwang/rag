import argparse
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from functools import partial
from uuid import UUID, uuid5

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .audit import emit
from .format_parser import PROFILES, ParseFailure, ParseReport
from .jobs import (
    LEASE_SECONDS, Lease, LeaseLost, complete, fail_in_transaction, lock_lease, run_worker, tenant_transaction,
)
from .models import EMBEDDING_BATCH_SIZE, BailianModels, ModelError
from .parse import IndexChunk, prepare_binary, version_config
from .policy import POLICY_ANCHORS, Policy, match_policy_anchors, read_metadata, section_policies
from .text import MAX_UPLOAD_BYTES, InvalidText, decode_text, split_text


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


def advance(conn: psycopg.Connection, lease: Lease, stage: str, report: ParseReport | None = None) -> None:
    with tenant_transaction(conn, lease):
        row = conn.execute(
            """
            UPDATE jobs SET stage = %s, deadline = clock_timestamp() + %s * interval '1 second',
                            parse_report = COALESCE(%s, parse_report)
            WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
            RETURNING id
            """,
            (stage, LEASE_SECONDS, Jsonb(asdict(report)) if report else None, lease.job, lease.token),
        ).fetchone()
        if row is None:
            raise LeaseLost


def publish(
    conn: psycopg.Connection, lease: Lease, source: Source,
    chunks: list[IndexChunk], vectors: list[list[float]], policies: dict[int, Policy],
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
                        Jsonb({**chunk.location_json(),
                               **({"policy": policies[ordinal].model_dump(mode="json")}
                                  if ordinal in policies else {})}),
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
    failed_report = None
    try:
        source = read_source(conn, lease)
        parser_config = source.config.copy()
        explicit_policy = parser_config.pop("policy", None)
        policy_anchors = parser_config.pop("policy_anchors", None)
        if parser_config != version_config(source.mime):
            raise InvalidText("Unsupported version configuration")
        original = source.content
        if len(original) > MAX_UPLOAD_BYTES or hashlib.sha256(original).digest() != source.checksum:
            raise InvalidText("Original checksum does not match")
        policies = {}
        if source.mime in PROFILES:
            advance(conn, lease, "parse")
            chunks, report = prepare_binary(source.mime, original, parser_config)
            if explicit_policy is not None:
                policy = Policy.model_validate(explicit_policy)
                policies = dict.fromkeys(range(len(chunks)), policy)
            elif policy_anchors is not None:
                anchors = POLICY_ANCHORS.validate_python(policy_anchors)
                matched = match_policy_anchors([chunk.body for chunk in chunks], anchors)
                if matched is None:
                    raise ParseFailure("policy_confirmation_required", report)
                policies = matched
            advance(conn, lease, "embed", report)
        else:
            markdown = source.mime == "text/markdown"
            if not markdown and source.mime != "text/plain":
                raise InvalidText("Unsupported original format")
            text = decode_text(original)
            chunks = split_text(text, markdown=markdown)
            if not chunks:
                raise InvalidText("Document contains no indexable text")
            try:
                by_heading = section_policies({chunk.heading for chunk in chunks}, read_metadata(text, markdown))
                policies = {i: by_heading[chunk.heading] for i, chunk in enumerate(chunks) if chunk.heading in by_heading}
            except ValueError as error:
                raise InvalidText("Invalid policy metadata") from error

        vectors = []
        for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            advance(conn, lease, "embed")
            batch = chunks[start:start + EMBEDDING_BATCH_SIZE]
            vectors.extend(models.embed([chunk.passage() for chunk in batch]))
        advance(conn, lease, "publish")
        published = publish(conn, lease, source, chunks, vectors, policies)
        emit("document.publish", source.trace, outcome="published" if published else "superseded",
             actor_type="app", actor=source.actor, service="ingest", tenant=lease.tenant,
             job=lease.job, library=source.library, document=source.document, version=source.version)
        return
    except LeaseLost:
        return
    except ParseFailure as error:
        reason, retryable = error.code, False
        failed_report = error.report
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
            if failed_report:
                conn.execute("UPDATE jobs SET parse_report = %s WHERE id = %s",
                             (Jsonb(asdict(failed_report)), lease.job))
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
