from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from .db import set_tenant

Action = Literal["read", "write"]


class InvalidKey(Exception):
    pass


class LibraryNotFound(Exception):
    pass


class LibraryForbidden(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    app: UUID
    tenant: UUID


@dataclass(frozen=True)
class Library:
    id: UUID
    code: str
    name: str


def key_digest(key: str) -> bytes:
    return sha256(key.encode()).digest()


@contextmanager
def app_session(pool: ConnectionPool, key: str) -> Iterator[tuple[psycopg.Connection, Identity]]:
    with pool.connection() as conn, conn.transaction():
        row = conn.execute("SELECT app, tenant FROM rag.resolve_key(%s)", (key_digest(key),)).fetchone()
        if row is None:
            raise InvalidKey

        identity = Identity(*row)
        set_tenant(conn, identity.tenant)
        yield conn, identity


def require_library(conn: psycopg.Connection, identity: Identity, code: str, action: Action) -> Library:
    row = conn.execute(
        """
        SELECT l.id, l.code, l.name, g.app IS NOT NULL AS allowed
        FROM libraries AS l
        LEFT JOIN grants AS g
          ON g.tenant = l.tenant AND g.library = l.id AND g.app = %s AND g.action = %s
        WHERE l.tenant = %s AND l.code = %s
        """,
        (identity.app, action, identity.tenant, code),
    ).fetchone()
    if row is None:
        raise LibraryNotFound
    library_id, library_code, name, allowed = row
    if not allowed:
        raise LibraryForbidden(action)
    return Library(library_id, library_code, name)
