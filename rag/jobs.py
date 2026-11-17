import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import sql

from .db import SCHEMA_OPTIONS, set_tenant

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 4
LEASE_SECONDS = 120
Kind = Literal["ingest", "sync"]


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    job: UUID
    tenant: UUID
    token: UUID


@contextmanager
def tenant_transaction(conn: psycopg.Connection, lease: Lease) -> Iterator[None]:
    with conn.transaction():
        set_tenant(conn, lease.tenant)
        yield


def claim(conn: psycopg.Connection, kind: Kind) -> Lease | None:
    query = sql.SQL("SELECT id, tenant, lease FROM {}(%s, %s)").format(sql.Identifier("rag", f"claim_{kind}"))
    row = conn.execute(query, (MAX_ATTEMPTS, LEASE_SECONDS)).fetchone()
    return Lease(*row) if row and row[2] else None


def lock_lease(conn: psycopg.Connection, lease: Lease) -> None:
    row = conn.execute(
        """
        SELECT id FROM jobs
        WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
        FOR UPDATE
        """,
        (lease.job, lease.token),
    ).fetchone()
    if row is None:
        raise LeaseLost


def complete(conn: psycopg.Connection, lease: Lease, stage: str) -> None:
    row = conn.execute(
        """
        UPDATE jobs SET state = 'done', stage = %s, lease = NULL, deadline = NULL, error = NULL
        WHERE id = %s AND lease = %s AND deadline > clock_timestamp()
        RETURNING id
        """,
        (stage, lease.job, lease.token),
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


def run_worker(dsn: str, kind: Kind, handle: Callable[[psycopg.Connection, Lease], None], *, once: bool) -> None:
    while True:
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5, options=SCHEMA_OPTIONS) as conn:
                while True:
                    lease = claim(conn, kind)
                    if lease:
                        handle(conn, lease)
                    if once:
                        return
                    if not lease:
                        time.sleep(1)
        except psycopg.Error as error:
            if once:
                raise SystemExit(f"Worker database operation failed ({type(error).__name__}).") from None
            logger.error("Worker database operation failed kind=%s error=%s", kind, type(error).__name__)
            time.sleep(2)
