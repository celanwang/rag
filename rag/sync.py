import argparse
import logging
import os
from dataclasses import dataclass
from functools import partial
from uuid import UUID

import httpx
import psycopg

from .audit import emit
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


def published_chunks(conn: psycopg.Connection, lease: Lease, document: Document) -> list[IndexChunk]:
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
        (lease.tenant, document.library, document.id, document.published),
    ).fetchall()
    if not rows:
        raise KeywordIndexError("Published version has no chunks to synchronize")
    return [IndexChunk(*row) for row in rows]


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
                chunks = published_chunks(conn, lease, document)
                index.replace_document(
                    lease.tenant, document.library, document.id, document.published, document.revision,
                    document.readers, chunks,
                )
                conn.execute("UPDATE documents SET synced = revision WHERE id = %s", (document.id,))
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
    parser.add_argument("--once", action="store_true", help="Claim at most one available task and exit")
    args = parser.parse_args()
    dsn = os.getenv("RAG_SYNC_DATABASE_URL")
    if not dsn:
        parser.error("set RAG_SYNC_DATABASE_URL before running the sync worker")

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        with KeywordIndex.from_env() as index:
            run_worker(dsn, "sync", partial(process, index=index), once=args.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
