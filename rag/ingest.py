import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .audit import emit
from .auth import Identity, app_session, require_library
from .format_parser import DOCX, PDF, PROFILES, REPORT, ParseFailure, ParseReport, matches_signature
from .parse import version_config
from .policy import Policy, PolicyAnchor, anchor_digest
from .text import InvalidText, decode_text

MIMES = {".md": "text/markdown", ".txt": "text/plain", ".docx": DOCX, ".pdf": PDF}


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
    parse_report: ParseReport | None = None


def upload_mime(filename: str) -> str:
    if any(ord(char) < 32 or char in "/\\" for char in filename):
        raise InvalidText("Expected a plain filename")
    mime = MIMES.get(PurePath(filename).suffix.lower())
    if mime is None:
        raise InvalidText("Expected a .md, .txt, .docx or .pdf file")
    return mime


def upload_fingerprint(
    filename: str, mime: str, document: UUID | None, checksum: bytes, policy: Policy | None,
) -> bytes:
    metadata = [filename, mime, str(document) if document else None]
    if policy is not None:
        metadata.append(policy.model_dump(mode="json"))
    return hashlib.sha256(checksum + json.dumps(metadata, ensure_ascii=False).encode()).digest()


def existing_submission(
    conn: psycopg.Connection, identity: Identity, library: UUID, key: str, fingerprint: bytes,
) -> Submission | None:
    row = conn.execute(
        """
        SELECT id, document, COALESCE(version, purged), fingerprint
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
    document: UUID | None, version: UUID, filename: str, mime: str, content: bytes, checksum: bytes, trace: UUID,
    policy: Policy | None,
) -> Submission:
    tenant = identity.tenant
    config = version_config(mime).copy()
    if policy is not None:
        config["policy"] = policy.model_dump(mode="json")
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
        if mime in PROFILES and policy is None:
            # Pin metadata from the version that is published when the replacement is accepted.
            rows = conn.execute("""
                SELECT c.body, c.location -> 'policy'
                FROM documents d JOIN chunks c ON (c.tenant, c.library, c.document, c.version) =
                                                  (d.tenant, d.library, d.id, d.published)
                WHERE d.id = %s AND d.library = %s AND c.location ->> 'policy' IS NOT NULL
                ORDER BY c.ordinal
            """, (document, library)).fetchall()
            if rows:
                config["policy_anchors"] = [
                    PolicyAnchor(digest=anchor_digest(body), policy=Policy.model_validate(data)).model_dump(mode="json")
                    for body, data in rows
                ]

    conn.execute(
        """
        INSERT INTO versions (id, tenant, library, document, number, filename, mime, content, checksum, config)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (version, tenant, library, document, number, filename, mime, content, checksum, Jsonb(config)),
    )
    conn.execute("UPDATE documents SET target = %s WHERE id = %s", (version, document))
    job = conn.execute(
        """
        INSERT INTO jobs (tenant, library, document, kind, app, version, request, fingerprint, stage, trace)
        VALUES (%s, %s, %s, 'ingest', %s, %s, %s, %s, 'accepted', %s) RETURNING id
        """,
        (tenant, library, document, identity.app, version, key, fingerprint, trace),
    ).fetchone()[0]
    return Submission(job, document, version)


def submit(
    pool: ConnectionPool, api_key: str, code: str, request_key: str,
    filename: str, body: bytes, document: UUID | None, trace: UUID, policy: Policy | None = None,
) -> Submission:
    mime = upload_mime(filename)
    if not matches_signature(mime, body):
        raise ParseFailure("format_mismatch")
    if mime not in PROFILES:
        if policy is not None:
            raise InvalidText("X-RAG-Policy is supported for DOCX and PDF uploads")
        decode_text(body)
    checksum = hashlib.sha256(body).digest()
    fingerprint = upload_fingerprint(filename, mime, document, checksum, policy)

    with app_session(pool, api_key) as (conn, identity):
        library = require_library(conn, identity, code, "write")
        lock = f"upload:{identity.tenant}:{library.id}:{identity.app}:{request_key}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock,))
        previous = existing_submission(conn, identity, library.id, request_key, fingerprint)
        if previous:
            return previous
        accepted = previous or record_submission(
            conn, identity, library.id, request_key, fingerprint, document, uuid4(), filename, mime, body, checksum, trace,
            policy,
        )
    emit("document.upload", trace, outcome="replayed" if previous else "accepted",
         actor_type="app", actor=identity.app, tenant=identity.tenant, library=library.id,
         job=accepted.job_id, document=accepted.document_id, version=accepted.version_id)
    return accepted


def job_status(pool: ConnectionPool, api_key: str, job: UUID) -> JobStatus:
    with app_session(pool, api_key) as (conn, identity):
        row = conn.execute(
            """
            SELECT j.id, j.document, COALESCE(j.version, j.purged), j.state, j.stage, j.attempts, j.error, j.available,
                   d.published,
                   CASE WHEN d.deleted IS NOT NULL AND d.synced = d.revision THEN 'synced'
                        WHEN d.published IS NULL THEN 'not_published'
                        WHEN d.synced = d.revision THEN 'synced'
                        WHEN s.state IN ('retry', 'failed') THEN s.state ELSE 'pending' END,
                   s.attempts, s.error, j.parse_report,
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
        report = REPORT.validate_python(row[-2]) if row[-2] is not None else None
        return JobStatus(*row[:-2], report)
