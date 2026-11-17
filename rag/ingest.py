import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .auth import Identity, app_session, require_library
from .text import CONFIG, InvalidText, decode_text

MIMES = {".md": "text/markdown", ".txt": "text/plain"}
logger = logging.getLogger(__name__)


class DocumentNotFound(Exception):
    pass


class JobNotFound(Exception):
    pass


class RequestConflict(Exception):
    pass


@dataclass(frozen=True)
class Submission:
    job_id: UUID
    document_id: UUID
    version_id: UUID


@dataclass(frozen=True)
class JobStatus:
    job_id: UUID
    document_id: UUID
    version_id: UUID
    state: str
    stage: str | None
    attempts: int
    error: str | None
    available_at: datetime
    published_version_id: UUID | None
    keyword_status: str
    keyword_attempts: int | None
    keyword_error: str | None


def upload_mime(filename: str) -> str:
    if any(ord(char) < 32 or char in "/\\" for char in filename):
        raise InvalidText("Expected a plain Markdown or text filename")
    mime = MIMES.get(PurePath(filename).suffix.lower())
    if mime is None:
        raise InvalidText("Expected a .md or .txt file")
    return mime


def upload_fingerprint(filename: str, mime: str, document: UUID | None, checksum: bytes) -> bytes:
    metadata = json.dumps([filename, mime, str(document) if document else None], ensure_ascii=False).encode()
    return hashlib.sha256(checksum + metadata).digest()


def existing_submission(
    conn: psycopg.Connection, identity: Identity, library: UUID, key: str, fingerprint: bytes,
) -> Submission | None:
    row = conn.execute(
        """
        SELECT id, document, version, fingerprint
        FROM jobs
        WHERE tenant = %s AND library = %s AND app = %s AND request = %s AND kind = 'ingest'
        """,
        (identity.tenant, library, identity.app, key),
    ).fetchone()
    if row is None:
        return None
    if row[3] != fingerprint:
        raise RequestConflict
    return Submission(*row[:3])


def lock_document(conn: psycopg.Connection, library: UUID, document: UUID) -> None:
    row = conn.execute(
        "SELECT id FROM documents WHERE library = %s AND id = %s AND deleted IS NULL FOR UPDATE",
        (library, document),
    ).fetchone()
    if row is None:
        raise DocumentNotFound


def record_submission(
    conn: psycopg.Connection, identity: Identity, library: UUID, key: str, fingerprint: bytes,
    document: UUID | None, version: UUID, filename: str, mime: str, content: bytes, checksum: bytes,
) -> Submission:
    tenant = identity.tenant
    if document is None:
        document = uuid4()
        conn.execute(
            "INSERT INTO documents (id, tenant, library) VALUES (%s, %s, %s)",
            (document, tenant, library),
        )
        number = 1
    else:
        lock_document(conn, library, document)
        number = conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM versions WHERE document = %s AND library = %s",
            (document, library),
        ).fetchone()[0]

    conn.execute(
        """
        INSERT INTO versions (id, tenant, library, document, number, filename, mime, content, checksum, config)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (version, tenant, library, document, number, filename, mime, content, checksum, Jsonb(CONFIG)),
    )
    conn.execute("UPDATE documents SET target = %s WHERE id = %s", (version, document))
    job = conn.execute(
        """
        INSERT INTO jobs (tenant, library, document, kind, app, version, request, fingerprint, stage)
        VALUES (%s, %s, %s, 'ingest', %s, %s, %s, %s, 'accepted') RETURNING id
        """,
        (tenant, library, document, identity.app, version, key, fingerprint),
    ).fetchone()[0]
    return Submission(job, document, version)


def submit(
    pool: ConnectionPool, api_key: str, code: str, request_key: str,
    filename: str, body: bytes, document: UUID | None,
) -> Submission:
    mime = upload_mime(filename)
    decode_text(body)
    checksum = hashlib.sha256(body).digest()
    fingerprint = upload_fingerprint(filename, mime, document, checksum)

    with app_session(pool, api_key) as (conn, identity):
        library = require_library(conn, identity, code, "write")
        lock = f"upload:{identity.tenant}:{library.id}:{identity.app}:{request_key}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock,))
        previous = existing_submission(conn, identity, library.id, request_key, fingerprint)
        if previous:
            return previous
        accepted = record_submission(
            conn, identity, library.id, request_key, fingerprint, document, uuid4(), filename, mime, body, checksum,
        )
    logger.info(
        "Upload accepted tenant=%s app=%s job=%s document=%s version=%s",
        identity.tenant, identity.app, accepted.job_id, accepted.document_id, accepted.version_id,
    )
    return accepted


def job_status(pool: ConnectionPool, api_key: str, job: UUID) -> JobStatus:
    with app_session(pool, api_key) as (conn, identity):
        row = conn.execute(
            """
            SELECT j.id, j.document, j.version, j.state, j.stage, j.attempts, j.error, j.available,
                   d.published,
                   CASE WHEN d.published IS NULL THEN 'not_published'
                        WHEN d.synced = d.revision THEN 'synced'
                        WHEN s.state IN ('retry', 'failed') THEN s.state ELSE 'pending' END,
                   s.attempts, s.error,
                   l.code
            FROM jobs AS j
            JOIN documents AS d ON (d.tenant, d.library, d.id) = (j.tenant, j.library, j.document)
            JOIN libraries AS l ON (l.tenant, l.id) = (j.tenant, j.library)
            LEFT JOIN jobs AS s ON (s.tenant, s.library, s.document) = (d.tenant, d.library, d.id)
                               AND s.kind = 'sync' AND s.revision = d.revision
            WHERE j.id = %s AND j.tenant = %s AND j.kind = 'ingest'
            """,
            (job, identity.tenant),
        ).fetchone()
        if row is None:
            raise JobNotFound
        require_library(conn, identity, row[-1], "write")
        return JobStatus(*row[:-1])
