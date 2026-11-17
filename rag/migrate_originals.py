"""Transfer originals from legacy rag-dev buckets into the version rows."""

import hashlib
import json

import psycopg

from .dev import RUNS, configure, container_env, read_local_env, runtime
from .text import MAX_UPLOAD_BYTES


def source_client(local: dict):
    from minio import Minio
    from urllib3 import PoolManager, Retry, Timeout

    legacy = container_env("rag-dev-objects-1")
    access_key = local.get("RAG_S3_ACCESS_KEY") or legacy.get("MINIO_ROOT_USER")
    secret_key = local.get("RAG_S3_SECRET_KEY") or legacy.get("MINIO_ROOT_PASSWORD")
    if not access_key or not secret_key:
        raise SystemExit("Start the legacy object service and restore its credentials in docs/env/rag/.env.")
    pool = PoolManager(timeout=Timeout(connect=3, read=10), retries=Retry(total=1))
    client = Minio(
        f"127.0.0.1:{local.get('RAG_S3_PORT', '59001')}",
        access_key=access_key, secret_key=secret_key,
        secure=False, region=local.get("RAG_S3_REGION", "us-east-1"), http_client=pool,
    )
    return client, pool


def migrate(conn: psycopg.Connection, bucket: str, local: dict) -> int | None:
    with conn.transaction():
        conn.execute("LOCK TABLE rag.versions IN ACCESS EXCLUSIVE MODE")
        legacy = conn.execute("""
            SELECT EXISTS (SELECT 1 FROM pg_attribute
                           WHERE attrelid = 'rag.versions'::regclass AND attname = 'path' AND NOT attisdropped)
        """).fetchone()[0]
        if not legacy:
            return None

        rows = conn.execute("SELECT id, path, checksum FROM rag.versions ORDER BY id").fetchall()
        conn.execute("ALTER TABLE rag.versions ADD COLUMN content bytea")
        if rows:
            client, pool = source_client(local)
            try:
                for version, path, checksum in rows:
                    response = client.get_object(bucket, path)
                    try:
                        content = response.read(MAX_UPLOAD_BYTES + 1)
                    finally:
                        response.close()
                        response.release_conn()
                    if not 1 <= len(content) <= MAX_UPLOAD_BYTES or hashlib.sha256(content).digest() != checksum:
                        raise RuntimeError(f"Original checksum or size mismatch for version {version}.")
                    conn.execute("UPDATE rag.versions SET content = %s WHERE id = %s", (content, version))
            finally:
                pool.clear()
        conn.execute("""
            ALTER TABLE rag.versions
                ALTER COLUMN content SET NOT NULL,
                ADD CONSTRAINT versions_content_check CHECK (octet_length(content) BETWEEN 1 AND 262144),
                DROP COLUMN path
        """)
        conn.execute("DROP POLICY IF EXISTS version_paths ON rag.versions")
        return len(rows)


def main() -> None:
    configure()
    local = read_local_env()
    buckets = {"rag": local.get("RAG_S3_BUCKET", "rag-originals")}
    for manifest in sorted(RUNS.glob("*/run.json")):
        run = json.loads(manifest.read_text())
        if run.get("bucket") and run["database"] != "rag":
            buckets[run["database"]] = run["bucket"]
    with psycopg.connect(runtime()["RAG_TEST_OWNER_URL"]) as conn:
        present = {row[0] for row in conn.execute("SELECT datname FROM pg_database")}
    for database, bucket in buckets.items():
        if database not in present:
            continue
        with psycopg.connect(runtime(database)["RAG_TEST_OWNER_URL"], autocommit=True, connect_timeout=5) as conn:
            if conn.execute("SELECT to_regclass('rag.versions') IS NOT NULL").fetchone()[0]:
                count = migrate(conn, bucket, local)
                print(f"{database}: {count} originals migrated" if count is not None else f"{database}: already migrated")


if __name__ == "__main__":
    main()
