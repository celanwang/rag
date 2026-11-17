import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient

from rag.api import app
from rag.db import SCHEMA_OPTIONS
from rag.jobs import claim
from rag.models import BailianModels
from rag.worker import process
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


class IngestionFixture:
    @classmethod
    def setUpClass(cls):
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
            WHERE tenant = ANY(%s::uuid[]) AND state IN ('queued', 'retry', 'running')
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
        return [content for (content,) in self.owner.execute(
            "SELECT content FROM rag.versions WHERE tenant = %s", (self.alpha_id,),
        )]

    def status(self, submission, key=None):
        return self.client.get(
            f"/v1/ingestion-jobs/{submission['job_id']}",
            headers={"Authorization": f"Bearer {key or self.writer['key']}"},
        )

    def process(self, job, status=200, lease=None):
        lease = lease or claim(self.worker, "ingest")
        self.assertIsNotNone(lease)
        self.assertEqual(str(lease.job), job["job_id"])
        with httpx.Client(base_url="https://example.invalid", transport=bailian_transport(status)) as client:
            process(self.worker, lease, BailianModels(client))
