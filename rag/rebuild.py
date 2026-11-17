"""Build a new keyword index from published PostgreSQL state and activate it after reconciliation."""
import argparse
import json
import os
import time
from contextlib import contextmanager
from uuid import UUID, uuid4

import httpx
import psycopg

from .db import SCHEMA_OPTIONS
from .keyword import KeywordIndex
from .sync import Document, published_chunks

PENDING = """
    FROM documents AS d LEFT JOIN index_generation_documents AS p
         ON p.document = d.id AND p.generation = %(name)s
    WHERE p.revision IS DISTINCT FROM d.revision
"""


@contextmanager
def generation_lock(conn, name):
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"rag-index:{name}",))
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (f"rag-index:{name}",))


def build(conn: psycopg.Connection, index: KeywordIndex, alias: str, batch: int, interval: float = 0.05) -> dict:
    name = index.path[1:]
    with generation_lock(conn, name):
        row = conn.execute("SELECT alias, status FROM index_generations WHERE name = %s", (name,)).fetchone()
        if row is None:
            if index.client.head(index.path).status_code != 404:
                raise ValueError("The new generation requires an unused Elasticsearch index name")
            conn.execute("INSERT INTO index_generations VALUES (%s, %s, 'building', DEFAULT)", (name, alias))
        elif row[0] != alias or row[1] == "active":
            raise ValueError("Cannot rebuild an active index or change a generation's alias")
        elif index.client.head(index.path).status_code == 404:
            conn.execute("DELETE FROM index_generation_documents WHERE generation=%s", (name,))
        index.initialize()
        pending = conn.execute(
            "SELECT d.id" + PENDING + " ORDER BY d.id LIMIT %(batch)s", {"name": name, "batch": batch},
        ).fetchall()
        for (document_id,) in pending:
            with conn.transaction():
                row = conn.execute("""
                    SELECT tenant, id, library, CASE WHEN deleted IS NULL THEN published END,
                           revision, synced, readers
                    FROM documents WHERE id = %s FOR UPDATE
                """, (document_id,)).fetchone()
                if row is None:
                    continue
                tenant, *state = row
                current = Document(*state, state[3], uuid4(), None)
                index.replace_document(
                    tenant, current.library, current.id, current.published, current.revision,
                    current.readers, published_chunks(conn, tenant, current),
                )
                conn.execute("""
                    INSERT INTO index_generation_documents (generation, tenant, library, document, revision)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (generation, document) DO UPDATE SET revision = excluded.revision
                """, (name, tenant, current.library, current.id, current.revision))
            if interval:
                time.sleep(interval)
        remaining = conn.execute("SELECT count(*)" + PENDING, {"name": name}).fetchone()[0]
        status = "building" if remaining else "ready"
        conn.execute("UPDATE index_generations SET status = %s WHERE name = %s", (status, name))
        return {"generation": name, "status": status, "processed": len(pending), "remaining": remaining}


def activate(conn: psycopg.Connection, index: KeywordIndex, alias: str) -> dict:
    name = index.path[1:]
    with generation_lock(conn, name), conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("LOCK TABLE documents IN EXCLUSIVE MODE")
        row = conn.execute("SELECT alias, status FROM index_generations WHERE name = %s", (name,)).fetchone()
        if row is None or row[0] != alias or row[1] not in ("ready", "active"):
            raise ValueError("Run a build for this generation and alias until it is ready")
        remaining = conn.execute("SELECT count(*)" + PENDING, {"name": name}).fetchone()[0]
        if remaining:
            raise ValueError(f"Generation has {remaining} newer document revisions to catch up")
        chunks = conn.execute("""
            SELECT count(*) FROM chunks c JOIN documents d ON
                (d.tenant,d.library,d.id,d.published) = (c.tenant,c.library,c.document,c.version)
            WHERE d.deleted IS NULL
        """).fetchone()[0]
        observed = index.client.get(f"{index.path}/_count").raise_for_status().json()
        if observed["count"] != chunks or observed["_shards"]["failed"]:
            raise ValueError("Generation chunk count differs from current PostgreSQL publication")
        lookup = index.client.get(f"/_alias/{alias}")
        previous = list(lookup.raise_for_status().json()) if lookup.status_code != 404 else []
        if len(previous) > 1:
            raise ValueError("Expected a single current index for this alias")
        if previous != [name]:
            actions = [{"remove": {"index": old, "alias": alias, "must_exist": True}} for old in previous]
            actions.append({"add": {"index": name, "alias": alias, "is_write_index": True}})
            result = index.client.post("/_aliases", json={"actions": actions}).raise_for_status().json()
            if not result.get("acknowledged") or result.get("errors"):
                raise RuntimeError("Elasticsearch did not confirm the alias switch")
        conn.execute("UPDATE index_generations SET status='archived' WHERE alias=%s AND status='active' AND name<>%s",
                     (alias, name))
        conn.execute("UPDATE index_generations SET status='active' WHERE name=%s", (name,))
        return {"generation": name, "status": "active", "previous": previous}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "activate"))
    parser.add_argument("alias")
    parser.add_argument("generation")
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--interval-ms", type=int, default=50)
    args = parser.parse_args()
    if args.alias == args.generation or not 1 <= args.batch <= 200 or not 0 <= args.interval_ms <= 10000:
        parser.error("Use distinct alias and generation names and a batch size from 1 to 200")
    with psycopg.connect(os.environ.get("RAG_INDEX_ADMIN_DATABASE_URL") or os.environ["RAG_TEST_OWNER_URL"],
                         autocommit=True, options=SCHEMA_OPTIONS) as conn, \
         KeywordIndex.from_env() as current:
        if args.action == "activate" and current.path != f"/{args.alias}":
            parser.error("Set RAG_ES_INDEX to the managed alias before activating")
        index = KeywordIndex(current.client, args.generation)
        KeywordIndex(current.client, args.alias)
        result = (build(conn, index, args.alias, args.batch, args.interval_ms / 1000)
                  if args.action == "build" else activate(conn, index, args.alias))
        print(json.dumps(result))


if __name__ == "__main__":
    main()
