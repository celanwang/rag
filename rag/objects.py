import argparse
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlsplit

import psycopg
from minio import Minio
from minio.error import InvalidResponseError, S3Error, ServerError
from urllib3 import PoolManager, Retry, Timeout
from urllib3.exceptions import HTTPError

from .db import SCHEMA_OPTIONS

OBJECT_ERRORS = (S3Error, InvalidResponseError, ServerError, HTTPError)


class StorageError(Exception):
    def __init__(self, retryable: bool):
        super().__init__("Object storage operation failed")
        self.retryable = retryable


def storage_error(error: Exception) -> StorageError:
    if isinstance(error, S3Error):
        status = error.response.status
        return StorageError(status in {408, 429} or status >= 500)
    return StorageError(retryable=True)


class ObjectStore:
    def __init__(self, client: Minio, pool: PoolManager, bucket: str):
        self.client = client
        self.pool = pool
        self.bucket = bucket

    @classmethod
    def from_env(cls) -> "ObjectStore":
        endpoint = urlsplit(os.environ["RAG_S3_ENDPOINT"])
        if (
            endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.path.rstrip("/") or endpoint.query or endpoint.fragment or endpoint.username is not None
        ):
            raise ValueError("RAG_S3_ENDPOINT must be an HTTP(S) origin")
        pool = PoolManager(timeout=Timeout(connect=3, read=10), retries=Retry(total=1))
        client = Minio(
            endpoint.netloc,
            access_key=os.environ["RAG_S3_ACCESS_KEY"], secret_key=os.environ["RAG_S3_SECRET_KEY"],
            secure=endpoint.scheme == "https", region=os.getenv("RAG_S3_REGION", "us-east-1"), http_client=pool,
        )
        return cls(client, pool, os.getenv("RAG_S3_BUCKET", "rag-originals"))

    def close(self) -> None:
        self.pool.clear()

    def put(self, key: str, content: bytes, mime: str) -> None:
        try:
            self.client.put_object(self.bucket, key, BytesIO(content), len(content), content_type=mime)
        except OBJECT_ERRORS as error:
            raise storage_error(error) from None

    def get(self, key: str, limit: int) -> bytes:
        try:
            body = self.client.get_object(self.bucket, key)
            try:
                return body.read(limit + 1)
            finally:
                body.close()
                body.release_conn()
        except OBJECT_ERRORS as error:
            raise storage_error(error) from None

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            try:
                self.client.make_bucket(self.bucket)
            except S3Error as error:
                if error.code != "BucketAlreadyOwnedByYou":
                    raise

    def prune(self, conn: psycopg.Connection, older_than: datetime) -> int:
        removed = 0
        for entry in self.client.list_objects(self.bucket, prefix="originals/", recursive=True):
            if entry.last_modified >= older_than:
                continue
            key = entry.object_name
            with conn.transaction():
                lock_object(conn, key)
                if conn.execute("SELECT 1 FROM versions WHERE path = %s", (key,)).fetchone() is None:
                    self.client.remove_object(self.bucket, key)
                    removed += 1
        return removed


def lock_object(conn: psycopg.Connection, key: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"object:{key}",))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the originals bucket.")
    parser.add_argument("--prune", action="store_true", help="Remove unreferenced originals older than 24 hours")
    args = parser.parse_args()
    store = ObjectStore.from_env()
    try:
        if args.prune:
            dsn = os.environ.get("RAG_ADMIN_DATABASE_URL")
            if not dsn:
                parser.error("set RAG_ADMIN_DATABASE_URL before pruning originals")
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5, options=SCHEMA_OPTIONS) as conn:
                removed = store.prune(conn, datetime.now(timezone.utc) - timedelta(hours=24))
            print(f"Unreferenced originals removed: {removed}")
        else:
            store.ensure_bucket()
            print(f"Object bucket ready: {store.bucket}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
