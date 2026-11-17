import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg

from ingestion_fixture import SAMPLES, IngestionFixture
from rag.db import SCHEMA_OPTIONS, set_tenant
from rag.jobs import MAX_ATTEMPTS, claim
from rag.text import MAX_UPLOAD_BYTES
from test_authorization import cli


class IngestionTest(IngestionFixture, unittest.TestCase):
    def test_upload_limits_and_concurrent_idempotency(self):
        self.assertEqual(self.upload(body=b"\xff").status_code, 422)
        self.assertEqual(self.upload(file="wrong.pdf", body=b"plain text").status_code, 422)
        self.assertEqual(self.upload(body=b"A" * (MAX_UPLOAD_BYTES + 1)).status_code, 413)
        with ThreadPoolExecutor(max_workers=4) as executor:
            uploads = list(executor.map(lambda _: self.accepted(), range(4)))
        self.assertTrue(all(upload == uploads[0] for upload in uploads))
        self.assertEqual(self.upload(body=b"different body").status_code, 409)
        self.assertEqual(self.status(uploads[0]).json()["state"], "queued")
        self.assertEqual(len(self.originals()), 1)

    def test_retry_then_publish_vectors_and_sync_task(self):
        file = "alpha/shipping.txt"
        job = self.accepted(file=file)
        self.process(job, status=429)
        retry = self.status(job).json()
        self.assertEqual((retry["state"], retry["error"], retry["attempts"]), ("retry", "embedding_unavailable", 1))
        self.assertIsNone(retry["published_version_id"])
        self.assertIsNone(claim(self.worker, "ingest"))

        self.owner.execute("UPDATE rag.jobs SET available = now() WHERE id = %s", (job["job_id"],))
        self.process(job)
        done = self.status(job).json()
        self.assertEqual((done["state"], done["attempts"], done["keyword_status"]), ("done", 2, "pending"))
        self.assertEqual(done["published_version_id"], job["version_id"])
        path, mime = self.owner.execute(
            "SELECT path, mime FROM rag.versions WHERE id = %s", (job["version_id"],),
        ).fetchone()
        self.assertEqual(mime, "text/plain")
        self.assertEqual(self.store.get(path, MAX_UPLOAD_BYTES), (SAMPLES / file).read_bytes())
        chunk_count, dimension, offsets = self.owner.execute(
            """
            SELECT count(*), min(vector_dims(embedding)), bool_and(location ? 'line_start' AND location ? 'line_end')
            FROM rag.chunks WHERE version = %s
            """,
            (job["version_id"],),
        ).fetchone()
        self.assertGreater(chunk_count, 0)
        self.assertEqual((dimension, offsets), (512, True))
        self.assertEqual(self.owner.execute(
            "SELECT revision, state FROM rag.jobs WHERE document = %s AND kind = 'sync'", (job["document_id"],),
        ).fetchall(), [(1, "queued")])

    def test_retry_budget_is_terminal(self):
        job = self.accepted()
        for _ in range(MAX_ATTEMPTS):
            self.process(job, status=503)
            self.owner.execute("UPDATE rag.jobs SET available = now() WHERE id = %s", (job["job_id"],))
        done = self.status(job).json()
        self.assertEqual((done["state"], done["attempts"]), ("failed", MAX_ATTEMPTS))
        self.assertIsNone(done["published_version_id"])
        self.assertIsNone(claim(self.worker, "ingest"))

    def test_failed_and_superseded_versions_preserve_publication(self):
        first = self.accepted()
        self.process(first)
        document = first["document_id"]
        invalid = self.accepted(request="invalid", document=document, body=b"# Heading only")
        self.process(invalid)
        failed = self.status(invalid).json()
        self.assertEqual((failed["state"], failed["error"]), ("failed", "content_invalid"))
        self.assertEqual(failed["published_version_id"], first["version_id"])
        stale = self.accepted(request="stale", document=document)
        stale_lease = claim(self.worker, "ingest")
        latest = self.accepted(request="latest", document=document, file="alpha/shipping.txt")
        self.process(latest)
        self.process(stale, lease=stale_lease)
        status = self.status(stale).json()
        self.assertEqual((status["state"], status["error"]), ("failed", "superseded"))
        self.assertEqual(status["published_version_id"], latest["version_id"])
        ids = self.owner.execute("SELECT DISTINCT version::text FROM rag.chunks WHERE document = %s", (document,))
        self.assertEqual({row[0] for row in ids}, {first["version_id"], latest["version_id"]})

    def test_expired_lease_is_reclaimed_and_fenced(self):
        job = self.accepted()
        first = claim(self.worker, "ingest")
        self.owner.execute("UPDATE rag.jobs SET deadline = now() WHERE id = %s", (job["job_id"],))
        second = claim(self.worker, "ingest")
        self.assertNotEqual(first.token, second.token)
        self.process(job, lease=first)
        self.assertEqual(self.status(job).json()["state"], "running")
        self.process(job, lease=second)
        status = self.status(job).json()
        self.assertEqual((status["state"], status["attempts"]), ("done", 2))

    def test_tenant_isolation_and_write_revocation(self):
        alpha = self.accepted()
        beta_key = self.beta_writer["key"]
        beta = self.accepted(file="beta/returns.md", key=beta_key)
        self.assertNotEqual(alpha["document_id"], beta["document_id"])
        self.assertEqual(self.status(alpha, key=beta_key).status_code, 404)
        self.assertEqual(self.status(beta).status_code, 404)
        self.assertEqual(self.upload(request="foreign", document=beta["document_id"]).status_code, 404)
        self.assertEqual(self.upload(key=self.reader).status_code, 403)
        self.assertEqual(self.status(alpha, key=self.reader).status_code, 403)
        self.process(alpha)
        self.process(beta)
        with self.worker.transaction():
            set_tenant(self.worker, self.alpha_id)
            self.assertEqual(self.worker.execute("SELECT tenant::text FROM versions").fetchall(), [(self.alpha_id,)])
        self.assertEqual(self.worker.execute("SELECT path FROM versions").fetchall(), [])
        cli("revoke", self.alpha, self.writer["id"], "shop", "write")
        self.assertEqual(self.status(alpha).status_code, 403)
        self.assertEqual(self.upload(request="again").status_code, 403)
        self.assertEqual(self.status(beta, key=beta_key).json()["state"], "done")

    def test_orphan_cleanup_preserves_referenced_original(self):
        job = self.accepted()
        path = self.owner.execute("SELECT path FROM rag.versions WHERE id = %s", (job["version_id"],)).fetchone()[0]
        orphan = f"originals/{self.alpha_id}/orphan"
        self.store.put(orphan, b"orphan", "text/plain")
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        with psycopg.connect(os.environ["RAG_ADMIN_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS) as conn:
            self.assertGreaterEqual(self.store.prune(conn, cutoff), 1)
        self.assertEqual(self.store.get(path, MAX_UPLOAD_BYTES), (SAMPLES / "alpha/returns.md").read_bytes())
        self.assertEqual(self.originals(), [path])
