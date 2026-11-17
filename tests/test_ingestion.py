import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient

from rag.api import app
from rag.db import SCHEMA_OPTIONS, set_tenant
from rag.models import BailianModels
from rag.objects import ObjectStore
from rag.text import MAX_UPLOAD_BYTES
from rag.worker import MAX_ATTEMPTS, claim, process
from test_authorization import cli

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "samples" / "ecommerce"


def bailian_transport(status):
    def respond(request):
        if status != 200:
            return httpx.Response(status)
        data = json.loads(request.content)
        assert data["model"] == "text-embedding-v4"
        assert data["parameters"] == {"dimension": 512, "text_type": "document"}
        rows = [
            {"text_index": index, "embedding": [0.25] * 512}
            for index in range(len(data["input"]["texts"]))
        ]
        return httpx.Response(200, json={"output": {"embeddings": rows}})
    return httpx.MockTransport(respond)


class IngestionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = ObjectStore.from_env()
        cls.addClassCleanup(cls.store.close)
        cls.store.ensure_bucket()
        cls.client = cls.enterClassContext(TestClient(app))

    def setUp(self):
        self.owner = self.enterContext(psycopg.connect(os.environ["RAG_TEST_OWNER_URL"], autocommit=True))
        self.worker = self.enterContext(psycopg.connect(
            os.environ["RAG_WORKER_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS,
        ))
        suffix = uuid4().hex[:12]
        self.alpha = f"shop-alpha-{suffix}"
        self.beta = f"shop-beta-{suffix}"
        self.alpha_id, self.writer = self.provision(self.alpha)
        self.beta_id, self.beta_writer = self.provision(self.beta)
        reader = cli("register-app", self.alpha, "Reader")
        cli("grant", self.alpha, reader["id"], "shop", "read")
        self.reader = reader["key"]
        self.addCleanup(self.clear_pending)

    def provision(self, tenant):
        tenant_id = cli("create-tenant", tenant, tenant)["id"]
        cli("create-library", tenant, "shop", "Shop")
        application = cli("register-app", tenant, "Writer")
        cli("grant", tenant, application["id"], "shop", "write")
        return tenant_id, application

    def clear_pending(self):
        # Keep subsequent tests from claiming unfinished jobs belonging to this fixture.
        self.owner.execute(
            """
            UPDATE rag.jobs SET state = 'failed', lease = NULL, deadline = NULL
            WHERE tenant = ANY(%s::uuid[]) AND kind = 'ingest' AND state IN ('queued', 'retry', 'running')
            """,
            ([self.alpha_id, self.beta_id],),
        )

    def upload(self, *, request="returns-1", file="alpha/returns.md", body=None, document=None, key=None):
        source = SAMPLES / file
        params = {"filename": source.name}
        if document is not None:
            params["document_id"] = document
        return self.client.post(
            "/v1/knowledge-bases/shop/documents", params=params,
            headers={
                "Authorization": f"Bearer {key or self.writer['key']}",
                "Idempotency-Key": request,
                "Content-Type": "application/octet-stream",
            },
            content=source.read_bytes() if body is None else body,
        )

    def accepted(self, **kwargs):
        response = self.upload(**kwargs)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def originals(self):
        return [
            item.object_name
            for item in self.store.client.list_objects(
                self.store.bucket, prefix=f"originals/{self.alpha_id}/", recursive=True,
            )
        ]

    def status(self, submission, key=None):
        return self.client.get(
            f"/v1/ingestion-jobs/{submission['job_id']}",
            headers={"Authorization": f"Bearer {key or self.writer['key']}"},
        )

    def process(self, job, status=200, lease=None):
        lease = lease or claim(self.worker)
        self.assertIsNotNone(lease)
        self.assertEqual(str(lease.job), job["job_id"])
        with httpx.Client(base_url="https://example.invalid", transport=bailian_transport(status)) as client:
            process(self.worker, lease, self.store, BailianModels(client))

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
        self.assertIsNone(claim(self.worker))

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
        self.assertIsNone(claim(self.worker))

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
        stale_lease = claim(self.worker)
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
        first = claim(self.worker)
        self.owner.execute("UPDATE rag.jobs SET deadline = now() WHERE id = %s", (job["job_id"],))
        second = claim(self.worker)
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
