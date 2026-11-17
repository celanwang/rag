"""Recompute policy inheritance on one published document from its stored original."""
import argparse
import hashlib
import json
import os
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .db import SCHEMA_OPTIONS
from .policy import read_metadata, section_policies
from .text import decode_text


def backfill(conn, tenant: UUID, document: UUID, *, apply: bool) -> dict:
    with conn.transaction():
        row = conn.execute("""
            SELECT d.published, v.mime, v.content, v.checksum
            FROM documents d JOIN versions v
              ON (v.tenant, v.library, v.document, v.id) = (d.tenant, d.library, d.id, d.published)
            WHERE d.tenant=%s AND d.id=%s AND d.deleted IS NULL
            FOR UPDATE OF d
        """, (tenant, document)).fetchone()
        if not row:
            raise ValueError("Published document not found in this tenant")
        version, mime, original, checksum = row
        if hashlib.sha256(original).digest() != checksum:
            raise ValueError("Original checksum mismatch")
        chunks = conn.execute("""
            SELECT id, COALESCE(heading, ''), location -> 'policy' FROM chunks
            WHERE tenant=%s AND document=%s AND version=%s
        """, (tenant, document, version)).fetchall()
        annotations = read_metadata(decode_text(original), mime == "text/markdown")
        policies = section_policies({heading for _, heading, _ in chunks}, annotations)
        changed = 0
        for chunk, heading, previous in chunks:
            policy = policies[heading].model_dump(mode="json") if heading in policies else None
            if previous == policy:
                continue
            changed += 1
            if apply:
                conn.execute("""
                    UPDATE chunks SET location = (location - 'policy') || %s
                    WHERE id=%s AND tenant=%s AND document=%s AND version=%s
                """, (Jsonb({"policy": policy} if policy else {}), chunk, tenant, document, version))
        return {"document_id": str(document), "version_id": str(version), "changes": changed, "applied": apply}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tenant", type=UUID)
    parser.add_argument("document", type=UUID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("RAG_INDEX_ADMIN_DATABASE_URL") or os.environ["RAG_TEST_OWNER_URL"]
    with psycopg.connect(dsn, autocommit=True, options=SCHEMA_OPTIONS) as conn:
        print(json.dumps(backfill(conn, args.tenant, args.document, apply=args.apply)))


if __name__ == "__main__":
    main()
