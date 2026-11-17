import json
import os
import unittest
from unittest.mock import patch

import httpx
import psycopg

from ingestion_fixture import IngestionFixture, SAMPLES
from rag.api import app
from rag.db import SCHEMA_OPTIONS
from rag.evidence import load_evidence, original
from rag.jobs import claim
from rag.keyword import KeywordIndex
from rag.models import BailianModels
from rag.sync import process as sync_revision
from test_authorization import cli


class RetrievalTest(IngestionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sync = self.enterContext(psycopg.connect(
            os.environ["RAG_SYNC_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS,
        ))
        self.index = self.enterContext(KeywordIndex.from_env())
        self.retriever = app.state.retriever
        self.reranked = []
        self.during_rerank = None
        self.invalid_scores = False
        model = self.enterContext(httpx.Client(
            base_url="https://example.invalid", transport=httpx.MockTransport(self.model_response),
        ))
        self.enterContext(patch.object(self.retriever, "models", BailianModels(model)))

    def model_response(self, request):
        data = json.loads(request.content)
        if data["model"] == "text-embedding-v4":
            self.assertEqual(data["parameters"], {"dimension": 512, "text_type": "query"})
            return httpx.Response(200, json={
                "output": {"embeddings": [{"text_index": 0, "embedding": [0.25] * 512}]},
            })
        self.assertEqual(data["model"], "qwen3-rerank")
        documents = data["input"]["documents"]
        self.reranked.extend(documents)
        if self.during_rerank:
            self.during_rerank()
        return httpx.Response(200, json={"output": {"results": [
            {"index": index, "relevance_score": None if self.invalid_scores else 1 / (index + 1)}
            for index in range(len(documents))
        ]}})

    @staticmethod
    def auth(key):
        return {"Authorization": f"Bearer {key}"}

    def publish(self, *, synchronize=True, **kwargs):
        job = self.accepted(**kwargs)
        self.process(job)
        if synchronize:
            self.sync_once()
        return job

    def sync_once(self):
        lease = claim(self.sync, "sync")
        self.assertIsNotNone(lease)
        sync_revision(self.sync, lease, self.index)

    def chunk(self, document):
        return self.owner.execute(
            "SELECT id FROM rag.chunks WHERE version = %s ORDER BY ordinal LIMIT 1", (document["version_id"],),
        ).fetchone()[0]

    def search(self, mode="rrf", *, key=None):
        return self.client.post("/v1/retrieve", headers=self.auth(key or self.reader), json={
            "knowledge_base_code": "shop", "question": "退货规则 RET-ALPHA-07", "mode": mode, "debug": True,
        })

    def set_readers(self, job, readers, *, key=None):
        return self.client.patch(
            f"/v1/documents/{job['document_id']}/readers", headers=self.auth(key or self.writer["key"]),
            json={"readers": readers},
        )

    def test_all_modes_citations_and_downloads_are_tenant_scoped(self):
        alpha = self.publish()
        beta = self.publish(file="beta/returns.md", key=self.beta_writer["key"])
        beta_reader = cli("register-app", self.beta, "Reader")
        cli("grant", self.beta, beta_reader["id"], "shop", "read")
        for mode in ("vector", "keyword", "rrf", "rerank"):
            response = self.search(mode)
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["degraded"], [])
            self.assertEqual({hit["document_id"] for hit in data["results"]}, {alpha["document_id"]})
        self.assertEqual(self.search(key=self.writer["key"]).status_code, 403)
        cite = f"/v1/citations/{self.chunk(alpha)}"
        download = f"/v1/documents/{alpha['document_id']}/file"
        response = self.client.get(cite, headers=self.auth(self.reader))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version_id"], alpha["version_id"])
        self.assertGreater(response.json()["line_start"], 0)
        file = self.client.get(download, headers=self.auth(self.reader))
        self.assertEqual(file.content, (SAMPLES / "alpha/returns.md").read_bytes())
        self.assertIn("attachment", file.headers["Content-Disposition"])
        for key in (beta_reader["key"], self.writer["key"]):
            self.assertEqual(self.client.get(cite, headers=self.auth(key)).status_code, 404)
            self.assertEqual(self.client.get(download, headers=self.auth(key)).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/citations/{self.chunk(beta)}", headers=self.auth(self.reader)).status_code, 404)

    def test_pg_discards_forged_tenant_and_stale_acl_before_reranking(self):
        permitted = self.publish()
        private = self.publish(request="shipping", file="alpha/shipping.txt")
        foreign = self.publish(file="beta/returns.md", key=self.beta_writer["key"])
        self.assertEqual(self.set_readers(private, [self.beta_writer["id"]]).status_code, 422)
        self.assertEqual(self.set_readers(private, [], key=self.reader).status_code, 404)
        self.assertEqual(self.set_readers(private, []).status_code, 200)
        authorized, denied, forged = map(self.chunk, (permitted, private, foreign))
        with patch.object(self.retriever.index, "search", return_value=[forged, denied, authorized]):
            response = self.search("rerank")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["debug"]["recall"]["keyword"], [str(authorized)])
        self.assertEqual({row["document_id"] for row in data["results"]}, {permitted["document_id"]})
        self.assertNotIn("SHIP-ALPHA-48", "".join(self.reranked))
        self.assertNotIn("RET-BETA-14", "".join(self.reranked))
        self.assertEqual(self.client.get(f"/v1/citations/{denied}", headers=self.auth(self.reader)).status_code, 404)
        self.assertEqual(self.client.get(
            f"/v1/documents/{private['document_id']}/file", headers=self.auth(self.reader),
        ).status_code, 404)

    def test_permission_change_during_rerank_is_rechecked(self):
        document = self.publish()
        self.during_rerank = lambda: self.owner.execute(
            "UPDATE rag.documents SET readers = '{}' WHERE id = %s", (document["document_id"],),
        )
        response = self.search("rerank")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.reranked)
        data = response.json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["debug"]["recall"], {"vector": [], "keyword": []})
        self.assertEqual(data["debug"]["prefinal"], [])

    def test_es_outage_and_postgres_authorization_failure(self):
        self.publish()
        with patch.object(self.retriever.index, "search", side_effect=httpx.ConnectError("unavailable")):
            self.assertEqual(self.search("keyword").status_code, 503)
            fallback = self.search("rrf").json()
            self.assertEqual(fallback["degraded"], ["keyword"])
            self.assertTrue(fallback["results"])
            vector = self.search("vector").json()
            self.assertEqual(fallback["results"], vector["results"])
        self.invalid_scores = True
        ranked = self.search("rerank").json()
        self.assertEqual(ranked["degraded"], ["rerank"])
        self.assertEqual(ranked["results"], self.search("rrf").json()["results"])
        calls = 0

        def fail_final(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise psycopg.OperationalError("PG unavailable")
            return load_evidence(*args, **kwargs)

        with patch("rag.retrieval.load_evidence", side_effect=fail_final):
            failure = self.search("keyword")
        self.assertEqual(failure.status_code, 503)
        self.assertNotIn("results", failure.json())

    def test_current_version_and_deleted_document_with_stale_index(self):
        first = self.publish()
        old_chunk = self.chunk(first)
        updated = self.publish(
            request="updated", document=first["document_id"], file="alpha/shipping.txt", synchronize=False,
        )
        self.assertEqual(self.search("keyword").json()["results"], [])
        self.assertEqual(self.client.get(f"/v1/citations/{old_chunk}", headers=self.auth(self.reader)).status_code, 404)
        download = f"/v1/documents/{first['document_id']}/file"
        self.assertEqual(self.client.get(download, headers=self.auth(self.reader)).content,
                         (SAMPLES / "alpha/shipping.txt").read_bytes())
        self.sync_once()
        self.assertEqual(self.client.get(f"/v1/citations/{self.chunk(updated)}",
                                         headers=self.auth(self.reader)).status_code, 200)
        path = f"/v1/documents/{first['document_id']}"
        self.assertEqual(self.client.delete(path, headers=self.auth(self.reader)).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=self.auth(self.writer["key"])).status_code, 202)
        self.assertEqual(self.client.get(download, headers=self.auth(self.reader)).status_code, 404)
        self.assertEqual(self.search("vector").json()["results"], [])
        self.assertEqual(self.search("keyword").json()["results"], [])
        self.sync_once()

    def test_revocation_during_download_discards_original(self):
        document = self.publish()
        download = f"/v1/documents/{document['document_id']}/file"
        def get_then_revoke(*args, **kwargs):
            source = original(*args, **kwargs)
            self.owner.execute("UPDATE rag.documents SET readers = '{}' WHERE id = %s", (document["document_id"],))
            return source

        with patch("rag.api.original", side_effect=get_then_revoke):
            response = self.client.get(download, headers=self.auth(self.reader))
        self.assertEqual(response.status_code, 404)
