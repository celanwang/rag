from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from .audit import emit
from .auth import Identity, app_session
from .ingest import DocumentNotFound


class InvalidReaders(Exception):
    pass


@dataclass(frozen=True)
class DocumentChange:
    document_id: UUID
    revision: int


def change_document(
    pool: ConnectionPool, key: str, document: UUID, trace: UUID, *, readers: list[UUID] | None = None, delete: bool = False,
) -> DocumentChange:
    event = "document.delete" if delete else "document.readers"
    with app_session(pool, key) as (conn, identity):
        try:
            library, revision, job = update_document(conn, identity, document, trace, readers, delete)
        except (DocumentNotFound, InvalidReaders) as error:
            emit(event, trace, outcome="rejected", actor_type="app", actor=identity.app,
                 tenant=identity.tenant, document=document, reason=type(error).__name__)
            raise
    fields = {} if delete else {"readers": readers}
    emit(event, trace, outcome="changed" if job else "unchanged",
         actor_type="app", actor=identity.app, tenant=identity.tenant, library=library,
         document=document, revision=revision, job=job, **fields)
    return DocumentChange(document, revision)


def update_document(
    conn: psycopg.Connection, identity: Identity, document: UUID, trace: UUID,
    readers: list[UUID] | None, delete: bool,
) -> tuple[UUID, int, UUID | None]:
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
            return library, revision, None
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
            return library, revision, None
        query = "UPDATE documents SET readers = %s, revision = revision + 1 WHERE id = %s RETURNING revision"
        params = (readers, document)

    revision = conn.execute(query, params).fetchone()[0]
    job = conn.execute(
        """
        INSERT INTO jobs (tenant, library, document, kind, revision, stage, app, trace)
        VALUES (%s, %s, %s, 'sync', %s, 'accepted', %s, %s) RETURNING id
        """,
        (identity.tenant, library, document, revision, identity.app, trace),
    ).fetchone()[0]
    return library, revision, job
