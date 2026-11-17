from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool

from .auth import Library, app_session, require_library
from .evidence import PUBLISHED, READABLE


@dataclass(frozen=True)
class Catalog:
    tenant_id: UUID
    libraries: list[Library]


@dataclass(frozen=True)
class DocumentSummary:
    document_id: UUID
    version_id: UUID
    filename: str
    mime: str
    version: int
    bytes: int
    synced: bool


@dataclass(frozen=True)
class DocumentPage:
    items: list[DocumentSummary]
    has_more: bool


def list_libraries(pool: ConnectionPool, key: str) -> Catalog:
    with app_session(pool, key) as (conn, identity):
        rows = conn.execute("""
            SELECT l.id, l.code, l.name
            FROM libraries AS l
            JOIN grants AS g ON (g.tenant, g.library) = (l.tenant, l.id)
            WHERE l.tenant = %s AND g.app = %s AND g.action = 'read'
            ORDER BY l.code
        """, (identity.tenant, identity.app)).fetchall()
    return Catalog(identity.tenant, [Library(*row) for row in rows])


def list_documents(
    pool: ConnectionPool, key: str, code: str, query: str, offset: int, limit: int,
) -> DocumentPage:
    with app_session(pool, key) as (conn, identity):
        library = require_library(conn, identity, code, "read")
        rows = conn.execute("""
            SELECT d.id, v.id, v.filename, v.mime, v.number, octet_length(v.content),
                   COALESCE(d.synced = d.revision, false)
        """ + PUBLISHED + READABLE + """
            AND d.library = %(library)s AND strpos(lower(v.filename), lower(%(query)s)) > 0
            ORDER BY v.filename, d.id OFFSET %(offset)s LIMIT %(limit)s
        """, {"tenant": identity.tenant, "app": identity.app, "library": library.id,
              "query": query, "offset": offset, "limit": limit + 1}).fetchall()
    return DocumentPage([DocumentSummary(*row) for row in rows[:limit]], len(rows) > limit)
