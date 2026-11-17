from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool

from .auth import app_session
from .ingest import DocumentNotFound


class InvalidReaders(Exception):
    pass


@dataclass(frozen=True)
class DocumentChange:
    document_id: UUID
    revision: int


def change_document(
    pool: ConnectionPool, key: str, document: UUID, *, readers: list[UUID] | None = None, delete: bool = False,
) -> DocumentChange:
    with app_session(pool, key) as (conn, identity):
        row = conn.execute(
            """
            SELECT d.library, d.readers, d.deleted, d.revision
            FROM documents AS d JOIN grants AS g ON (g.tenant, g.library) = (d.tenant, d.library)
            WHERE d.tenant = %s AND d.id = %s AND g.app = %s AND g.action = 'write'
            FOR UPDATE OF d
            """,
            (identity.tenant, document, identity.app),
        ).fetchone()
        if row is None:
            raise DocumentNotFound
        library, previous, deleted, revision = row
        if deleted is not None:
            if delete:
                return DocumentChange(document, revision)
            raise DocumentNotFound

        if delete:
            query = "UPDATE documents SET deleted = now(), revision = revision + 1 WHERE id = %s RETURNING revision"
            params = (document,)
        else:
            if readers is not None:
                readers = sorted(set(readers))
                members = conn.execute(
                    "SELECT id FROM apps WHERE tenant = %s AND id = ANY(%s::uuid[])",
                    (identity.tenant, readers),
                ).fetchall()
                if len(members) != len(readers):
                    raise InvalidReaders
            if readers == previous:
                return DocumentChange(document, revision)
            query = "UPDATE documents SET readers = %s, revision = revision + 1 WHERE id = %s RETURNING revision"
            params = (readers, document)

        revision = conn.execute(query, params).fetchone()[0]
        conn.execute(
            """
            INSERT INTO jobs (tenant, library, document, kind, revision, stage)
            VALUES (%s, %s, %s, 'sync', %s, 'accepted')
            """,
            (identity.tenant, library, document, revision),
        )
        return DocumentChange(document, revision)
