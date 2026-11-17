import argparse
import logging
import os
from dataclasses import dataclass
from functools import partial
from uuid import UUID, uuid4

import httpx
import psycopg

from .audit import emit
from .db import SCHEMA_OPTIONS, set_tenant
from .jobs import Lease, LeaseLost, complete, fail_in_transaction, lock_lease, run_worker, tenant_transaction
from .keyword import IndexChunk, KeywordIndex, KeywordIndexError

@dataclass(frozen=True)
class Document:
    id: UUID
    library: UUID
    published: UUID | None
    revision: int
    synced: int | None
    readers: list[UUID] | None
    requested: int
    trace: UUID
    actor: UUID | None


def lock_document(conn: psycopg.Connection, lease: Lease) -> Document:
    # Use the same row as publication so concurrent workers always index the current revision in order.
    row = conn.execute(
        """
        SELECT d.id, d.library, CASE WHEN d.deleted IS NULL THEN d.published END,
               d.revision, d.synced, d.readers, j.revision, COALESCE(j.trace, j.id), j.app
        FROM jobs AS j
        JOIN documents AS d ON (d.tenant, d.library, d.id) = (j.tenant, j.library, j.document)
        WHERE j.id = %s AND j.lease = %s AND j.deadline > clock_timestamp() AND j.kind = 'sync'
        FOR UPDATE OF d
        """,
        (lease.job, lease.token),
    ).fetchone()
    if row is None:
        raise LeaseLost
    lock_lease(conn, lease)
    return Document(*row)


def published_chunks(conn: psycopg.Connection, tenant: UUID, document: Document) -> list[IndexChunk]:
    if document.published is None:
        return []
    rows = conn.execute(
        """
        SELECT c.id, v.filename, c.heading, c.body
        FROM chunks AS c
        JOIN versions AS v ON (v.tenant, v.library, v.document, v.id) = (c.tenant, c.library, c.document, c.version)
        WHERE c.tenant = %s AND c.library = %s AND c.document = %s AND c.version = %s
        ORDER BY c.ordinal
        """,
        (tenant, document.library, document.id, document.published),
    ).fetchall()
    if not rows:
        raise KeywordIndexError("Published version has no chunks to synchronize")
    return [IndexChunk(*row) for row in rows]


def index_document(conn: psycopg.Connection, tenant: UUID, document: Document, index: KeywordIndex) -> None:
    chunks = published_chunks(conn, tenant, document)
    index.replace_document(
        tenant, document.library, document.id, document.published, document.revision, document.readers, chunks,
    )
    conn.execute("UPDATE documents SET synced = revision WHERE id = %s", (document.id,))


def repair(conn: psycopg.Connection, tenant: UUID, document_id: UUID, index: KeywordIndex) -> bool:
    trace = uuid4()
    with conn.transaction():
        set_tenant(conn, tenant)
        row = conn.execute("""
            SELECT id, library, CASE WHEN deleted IS NULL THEN published END, revision, synced, readers
            FROM documents WHERE tenant = %s AND id = %s FOR UPDATE
        """, (tenant, document_id)).fetchone()
        if row is None:
            return False
        document = Document(*row, row[3], trace, None)
        index_document(conn, tenant, document, index)
    emit("document.sync", trace, outcome="repaired", actor_type="service", actor="sync", service="sync",
         tenant=tenant, library=document.library, document=document.id, revision=document.revision)
    return True


def process(conn: psycopg.Connection, lease: Lease, index: KeywordIndex) -> None:
    try:
        with tenant_transaction(conn, lease):
            document = lock_document(conn, lease)
            trace, actor = document.trace, document.actor
            if document.synced == document.revision:
                outcome = "covered"
            else:
                if document.revision != document.requested:
                    cause = conn.execute("""
                        SELECT COALESCE(trace, id), app FROM jobs
                        WHERE document = %s AND revision = %s AND kind = 'sync'
                    """, (document.id, document.revision)).fetchone()
                    if cause is not None:
                        trace, actor = cause
                index_document(conn, lease.tenant, document, index)
                outcome = "indexed"
            complete(conn, lease, outcome)
        emit("document.sync", trace, outcome=outcome, actor_type="app" if actor else "service",
             actor=actor or "sync", service="sync", tenant=lease.tenant, job=lease.job,
             library=document.library, document=document.id, revision=document.revision)
        return
    except LeaseLost:
        return
    except httpx.RequestError:
        reason, retryable = "index_unavailable", True
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        retryable = status in {408, 409, 429} or status >= 500
        reason = "index_unavailable" if retryable else "index_rejected"
    except KeywordIndexError:
        reason, retryable = "index_incomplete", True

    try:
        with tenant_transaction(conn, lease):
            state = fail_in_transaction(conn, lease, reason, retryable)
        emit("document.sync", trace, outcome=state, actor_type="app" if actor else "service",
             actor=actor or "sync", service="sync", tenant=lease.tenant, job=lease.job, library=document.library,
             document=document.id, revision=document.revision, error=reason)
    except LeaseLost:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize published versions to Elasticsearch.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--once", action="store_true", help="Claim at most one available task and exit")
    action.add_argument("--repair", nargs=2, metavar=("TENANT_ID", "DOCUMENT_ID"), type=UUID,
                        help="Repair one document using its current PostgreSQL version and permissions")
    args = parser.parse_args()
    dsn = os.getenv("RAG_SYNC_DATABASE_URL")
    if not dsn:
        parser.error("set RAG_SYNC_DATABASE_URL before running the sync worker")

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        with KeywordIndex.from_env() as index:
            if args.repair:
                with psycopg.connect(dsn, autocommit=True, connect_timeout=5, options=SCHEMA_OPTIONS) as conn:
                    if not repair(conn, *args.repair, index):
                        parser.exit(1, "Document not found in this tenant\n")
            else:
                run_worker(dsn, "sync", partial(process, index=index), once=args.once)
    except KeyboardInterrupt:
        pass
    except (httpx.HTTPError, KeywordIndexError, psycopg.Error) as error:
        parser.exit(1, f"Repair failed ({type(error).__name__})\n")


if __name__ == "__main__":
    main()
