import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from uuid import UUID

import psycopg
from pydantic import TypeAdapter
from psycopg_pool import ConnectionPool

from .auth import Identity, InvalidKey, Library, LibraryNotFound, app_session, require_library
from .locations import Location, TextLocation
from .policy import Policy
from .text import model_passage

PUBLISHED = """
    FROM documents AS d
    JOIN versions AS v ON (v.tenant, v.library, v.document, v.id) = (d.tenant, d.library, d.id, d.published)
    JOIN grants AS g ON (g.tenant, g.library) = (d.tenant, d.library)
                     AND g.app = %(app)s AND g.action = 'read'
"""
CHUNKS = """
    JOIN chunks AS c ON (c.tenant, c.library, c.document, c.version) = (d.tenant, d.library, d.id, d.published)
"""
READABLE = """
    WHERE d.tenant = %(tenant)s AND d.deleted IS NULL
      AND (d.readers IS NULL OR %(app)s = ANY(d.readers))
"""
EVIDENCE = """
    SELECT c.id, c.document, c.version, v.filename, c.heading, c.body,
           (c.location ->> 'line_start')::int, (c.location ->> 'line_end')::int,
           c.location -> 'policy', c.location -> 'source'
""" + PUBLISHED + CHUNKS + READABLE


class CitationNotFound(Exception):
    pass


@dataclass(frozen=True)
class ReadScope:
    identity: Identity
    library: Library

    def params(self) -> dict[str, UUID]:
        return {"tenant": self.identity.tenant, "app": self.identity.app, "library": self.library.id}


@dataclass(frozen=True)
class Evidence:
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    filename: str
    heading: str | None
    body: str
    line_start: int | None
    line_end: int | None
    policy_data: dict | None = None
    location: Location | None = None

    def passage(self) -> str:
        return model_passage(self.heading, self.body)

    def policy(self) -> Policy | None:
        return Policy.model_validate(self.policy_data) if self.policy_data else None


LOCATOR = TypeAdapter(Location)


def evidence_from_row(row: tuple) -> Evidence:
    *fields, source = row
    item = Evidence(*fields)
    if source is not None:
        return replace(item, location=LOCATOR.validate_python(source))
    if item.line_start is not None and item.line_end is not None:
        return replace(item, location=TextLocation(item.line_start, item.line_end))
    return item


@dataclass(frozen=True)
class Original:
    version_id: UUID
    filename: str
    mime: str
    content: bytes
    checksum: bytes


def read_scope(pool: ConnectionPool, key: str, code: str) -> ReadScope:
    with app_session(pool, key) as (conn, identity):
        return ReadScope(identity, require_library(conn, identity, code, "read"))


@contextmanager
def read_session(pool: ConnectionPool, key: str, scope: ReadScope) -> Iterator[psycopg.Connection]:
    with app_session(pool, key) as (conn, identity):
        if identity != scope.identity:
            raise InvalidKey
        if require_library(conn, identity, scope.library.code, "read").id != scope.library.id:
            raise LibraryNotFound
        yield conn


def vector_candidates(
    pool: ConnectionPool, key: str, scope: ReadScope, embedding: list[float], limit: int,
) -> list[UUID]:
    with read_session(pool, key, scope) as conn:
        rows = conn.execute(
            "SELECT c.id" + PUBLISHED + CHUNKS + READABLE + """
            AND c.library = %(library)s
            ORDER BY c.embedding OPERATOR(public.<=>) %(embedding)s::public.vector, c.id
            LIMIT %(limit)s
            """,
            scope.params() | {"embedding": json.dumps(embedding), "limit": limit},
        ).fetchall()
    return [row[0] for row in rows]


def load_evidence(pool: ConnectionPool, key: str, scope: ReadScope, ids: set[UUID]) -> dict[UUID, Evidence]:
    with read_session(pool, key, scope) as conn:
        rows = conn.execute(
            EVIDENCE + "AND c.library = %(library)s AND c.id = ANY(%(ids)s::uuid[])",
            scope.params() | {"ids": list(ids)},
        ).fetchall()
    return {row[0]: evidence_from_row(row) for row in rows}


def neighbors(pool: ConnectionPool, key: str, scope: ReadScope, anchors: list[UUID]) -> dict[UUID, Evidence]:
    with read_session(pool, key, scope) as conn:
        rows = conn.execute(
            EVIDENCE + """
            AND c.library = %(library)s AND EXISTS (
                SELECT 1 FROM chunks AS anchor
                WHERE (anchor.tenant, anchor.library, anchor.document, anchor.version) =
                      (c.tenant, c.library, c.document, c.version)
                  AND anchor.id = ANY(%(anchors)s::uuid[])
                  AND anchor.location ->> 'section_start' = c.location ->> 'section_start'
                  AND c.ordinal BETWEEN anchor.ordinal - 2 AND anchor.ordinal + 2
            )
            AND NOT c.id = ANY(%(anchors)s::uuid[])
            ORDER BY c.document, c.ordinal
            """, scope.params() | {"anchors": anchors},
        ).fetchall()
    return {row[0]: evidence_from_row(row) for row in rows}


def citation(pool: ConnectionPool, key: str, chunk: UUID) -> Evidence:
    with app_session(pool, key) as (conn, identity):
        row = conn.execute(
            EVIDENCE + "AND c.id = %(chunk)s",
            {"tenant": identity.tenant, "app": identity.app, "chunk": chunk},
        ).fetchone()
    if row is None:
        raise CitationNotFound
    return evidence_from_row(row)


def original(pool: ConnectionPool, key: str, document: UUID, version: UUID | None = None) -> Original | None:
    with app_session(pool, key) as (conn, identity):
        params = {"tenant": identity.tenant, "app": identity.app, "document": document}
        query = ("SELECT v.id, v.filename, v.mime, v.content, v.checksum" + PUBLISHED + READABLE
                 + "AND d.id = %(document)s")
        if version is not None:
            query += " AND v.id = %(version)s"
            params["version"] = version
        row = conn.execute(query, params).fetchone()
    return Original(*row) if row is not None else None


def original_is_current(pool: ConnectionPool, key: str, document: UUID, version: UUID) -> bool:
    with app_session(pool, key) as (conn, identity):
        row = conn.execute(
            "SELECT 1" + PUBLISHED + READABLE + "AND d.id = %(document)s AND v.id = %(version)s",
            {"tenant": identity.tenant, "app": identity.app, "document": document, "version": version},
        ).fetchone()
    return row is not None
