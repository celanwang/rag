from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg

from .audit import emit_operator
from .cli import tenant_id

ELIGIBLE = """
    SELECT v.id, d.id
    FROM versions AS v
    JOIN documents AS d ON (d.tenant, d.library, d.id) = (v.tenant, v.library, v.document)
    WHERE v.tenant = %(tenant)s
      AND (
        (d.deleted IS NULL AND v.retired <= %(cutoff)s
            AND v.id IS DISTINCT FROM d.target AND v.id IS DISTINCT FROM d.published)
        OR (d.deleted <= %(cutoff)s AND d.synced = d.revision)
      )
      AND NOT EXISTS (
        SELECT 1 FROM jobs AS j WHERE j.version = v.id AND j.state NOT IN ('done', 'failed')
      )
"""


def retain(conn: psycopg.Connection, code: str, *, days: int, limit: int, apply: bool) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with conn.transaction():
        tenant = tenant_id(conn, code)
        params = {"tenant": tenant, "cutoff": cutoff, "limit": limit}
        order = " ORDER BY d.id, v.number LIMIT %(limit)s"
        lock = " FOR UPDATE OF d SKIP LOCKED" if apply else ""
        rows = conn.execute(ELIGIBLE + order + lock, params).fetchall()
        if apply and rows:
            # Recheck after locking the document used by upload, publication, deletion and sync.
            ids = [version for version, _ in rows]
            rows = conn.execute(ELIGIBLE + " AND v.id = ANY(%(ids)s)", params | {"ids": ids}).fetchall()

        versions = [version for version, _ in rows]
        documents = {document for _, document in rows}
        scope = (tenant, versions)
        if apply and versions:
            conn.execute("""
                UPDATE documents
                SET target = CASE WHEN target = ANY(%s) THEN NULL ELSE target END,
                    published = CASE WHEN published = ANY(%s) THEN NULL ELSE published END
                WHERE tenant = %s AND deleted IS NOT NULL AND (target = ANY(%s) OR published = ANY(%s))
            """, (versions, versions, tenant, versions, versions))
            conn.execute("""
                UPDATE jobs SET purged = version, version = NULL
                WHERE tenant = %s AND version = ANY(%s) AND state IN ('done', 'failed')
            """, scope)
            chunks = conn.execute("DELETE FROM chunks WHERE tenant = %s AND version = ANY(%s)", scope).rowcount
            conn.execute("DELETE FROM versions WHERE tenant = %s AND id = ANY(%s)", scope)
        else:
            chunks = conn.execute("SELECT count(*) FROM chunks WHERE tenant = %s AND version = ANY(%s)", scope).fetchone()[0]

    result = {"tenant": code, "cutoff": cutoff.isoformat(), "applied": apply,
              "documents": len(documents), "versions": len(versions), "chunks": chunks}
    if apply:
        trace = uuid4()
        for version, document in rows:
            emit_operator("document.purge", trace, outcome="deleted", tenant=tenant, document=document, version=version)
        emit_operator("retention.apply", trace, outcome="completed", tenant=tenant, tenant_code=code,
                      versions=len(versions), chunks=chunks, cutoff=cutoff.isoformat())
        result["trace_id"] = str(trace)
    return result
