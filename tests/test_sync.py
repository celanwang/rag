import os
import unittest
from uuid import UUID

import httpx
import psycopg

from ingestion_fixture import SAMPLES, IngestionFixture
from rag.db import SCHEMA_OPTIONS, set_tenant
from rag.jobs import MAX_ATTEMPTS, claim
from rag.keyword import KeywordIndex, scope_filters
from rag.sync import process


class SyncTest(IngestionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sync = self.enterContext(psycopg.connect(
            os.environ["RAG_SYNC_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS,
        ))
        self.index = self.enterContext(KeywordIndex.from_env())

    def publish(self, **kwargs):
        job = self.accepted(**kwargs)
        self.process(job)
        self.assertEqual(self.status(job, key=kwargs.get("key")).json()["keyword_status"], "pending")
        return job

    def sync_job(self, ingest, *, lease=None, index=None):
        lease = lease or claim(self.sync, "sync")
        self.assertIsNotNone(lease)
        expected = self.owner.execute(
            "SELECT document::text FROM rag.jobs WHERE id = %s AND kind = 'sync'", (lease.job,),
        ).fetchone()[0]
        self.assertEqual(expected, ingest["document_id"])
        process(self.sync, lease, index or self.index)

    def library(self, tenant):
        return self.owner.execute(
            "SELECT id FROM rag.libraries WHERE tenant = %s AND code = 'shop'", (tenant,),
        ).fetchone()[0]

    def candidates(self, question, *, tenant=None, library=None, reader=None):
        tenant = tenant or self.alpha_id
        return self.index.search(
            UUID(tenant), library or self.library(tenant), UUID(reader or self.writer["id"]), question,
        )

    def stored_chunks(self, tenant, document):
        result = self.index.client.post(f"{self.index.path}/_search", json={
            "size": 100,
            "query": {"bool": {"filter": [
                *scope_filters(UUID(tenant), self.library(tenant)),
                {"term": {"document_id": document}},
            ]}},
        })
        result.raise_for_status()
        return [hit["_source"] for hit in result.json()["hits"]["hits"]]

    def test_latest_version_is_searchable_and_old_chunks_are_removed(self):
        alpha = self.publish()
        beta = self.publish(file="beta/returns.md", key=self.beta_writer["key"])
        self.sync_job(alpha)
        self.sync_job(beta)
        before = self.status(alpha).json()
        self.assertEqual((before["keyword_status"], before["keyword_attempts"]), ("synced", 1))
        beta_before = self.stored_chunks(self.beta_id, beta["document_id"])
        self.assertTrue(beta_before)
        self.assertTrue(self.candidates("RET-ALPHA-07"))
        self.assertTrue(self.candidates("RET-BETA-14", tenant=self.beta_id, reader=self.beta_writer["id"]))
        self.assertEqual(self.candidates("RET-BETA-14", library=self.library(self.beta_id)), [])

        body = (SAMPLES / "alpha" / "returns.md").read_bytes().replace(b"RET-ALPHA-07", b"RET-ALPHA-30")
        updated = self.publish(request="returns-2", document=alpha["document_id"], body=body)
        self.sync_job(updated)
        sources = self.stored_chunks(self.alpha_id, alpha["document_id"])
        self.assertTrue(sources)
        self.assertEqual({source["version_id"] for source in sources}, {updated["version_id"]})
        self.assertFalse(any("RET-ALPHA-07" in source["identifiers"] for source in sources))
        self.assertTrue(self.candidates("RET-ALPHA-30"))
        self.assertEqual(self.stored_chunks(self.beta_id, beta["document_id"]), beta_before)
        self.assertEqual(self.status(updated).json()["keyword_status"], "synced")

    def test_retries_incomplete_bulk_and_failed_cleanup(self):
        initial = self.publish()
        self.sync_job(initial)
        updated = self.publish(request="shipping", document=initial["document_id"], file="alpha/shipping.txt")
        lease = claim(self.sync, "sync")

        def fail_bulk(request):
            if request.url.path.endswith("/_bulk"):
                return httpx.Response(200, json={"errors": True, "items": []})
            return self.index.client.send(request)

        with httpx.Client(base_url=self.index.client.base_url, transport=httpx.MockTransport(fail_bulk)) as client:
            self.sync_job(updated, lease=lease, index=KeywordIndex(client, self.index.path[1:]))
        failed_bulk = self.status(updated).json()
        self.assertEqual(
            (failed_bulk["keyword_status"], failed_bulk["keyword_attempts"], failed_bulk["keyword_error"]),
            ("retry", 1, "index_incomplete"),
        )
        self.assertEqual({row["version_id"] for row in self.stored_chunks(self.alpha_id, initial["document_id"])},
                         {initial["version_id"]})
        self.owner.execute("UPDATE rag.jobs SET available = now() WHERE id = %s", (lease.job,))
        lease = claim(self.sync, "sync")

        def fail_cleanup(request):
            if request.url.path.endswith("/_delete_by_query"):
                return httpx.Response(503)
            return self.index.client.send(request)

        with httpx.Client(base_url=self.index.client.base_url, transport=httpx.MockTransport(fail_cleanup)) as client:
            self.sync_job(updated, lease=lease, index=KeywordIndex(client, self.index.path[1:]))
        failed_cleanup = self.status(updated).json()
        self.assertEqual(
            (failed_cleanup["keyword_status"], failed_cleanup["keyword_attempts"], failed_cleanup["keyword_error"]),
            ("retry", 2, "index_unavailable"),
        )
        current = self.owner.execute("SELECT synced FROM rag.documents WHERE id = %s", (initial["document_id"],))
        self.assertEqual(current.fetchone()[0], 1)
        sources = self.stored_chunks(self.alpha_id, initial["document_id"])
        self.assertEqual({source["version_id"] for source in sources}, {initial["version_id"], updated["version_id"]})

        self.owner.execute("UPDATE rag.jobs SET available = now() WHERE id = %s", (lease.job,))
        self.sync_job(updated)
        result = self.status(updated).json()
        self.assertEqual((result["keyword_status"], result["keyword_attempts"], result["keyword_error"]),
                         ("synced", 3, None))
        self.assertTrue(self.candidates("SHIP-ALPHA-48"))
        self.assertEqual({row["version_id"] for row in self.stored_chunks(self.alpha_id, initial["document_id"])},
                         {updated["version_id"]})

    def test_out_of_order_jobs_and_expired_leases(self):
        initial = self.publish()
        stale = claim(self.sync, "sync")
        updated = self.publish(request="new", document=initial["document_id"], file="alpha/shipping.txt")
        expired = claim(self.sync, "sync")
        self.owner.execute("UPDATE rag.jobs SET deadline = now() WHERE id = %s", (expired.job,))
        renewed = claim(self.sync, "sync")
        self.assertEqual(expired.job, renewed.job)
        self.assertNotEqual(expired.token, renewed.token)
        self.sync_job(updated, lease=expired)
        self.assertEqual(self.status(updated).json()["keyword_status"], "pending")
        self.sync_job(updated, lease=renewed)
        self.sync_job(initial, lease=stale)
        self.assertEqual(self.owner.execute("SELECT stage FROM rag.jobs WHERE id = %s", (stale.job,)).fetchone()[0],
                         "covered")
        self.assertEqual({row["version_id"] for row in self.stored_chunks(self.alpha_id, initial["document_id"])},
                         {updated["version_id"]})

    def test_acl_and_deleted_documents_replace_index_state(self):
        document = self.publish()
        self.sync_job(document)
        self.assertTrue(self.candidates("RET-ALPHA-07"))
        self.change_document(document, readers=[])
        self.sync_job(document)
        self.assertEqual(self.candidates("RET-ALPHA-07"), [])
        self.change_document(document, readers=[self.writer["id"]])
        self.sync_job(document)
        self.assertTrue(self.candidates("RET-ALPHA-07"))
        self.assertEqual(self.candidates("RET-ALPHA-07", reader=self.beta_writer["id"]), [])
        self.change_document(document, deleted=True)
        self.sync_job(document)
        self.assertEqual(self.stored_chunks(self.alpha_id, document["document_id"]), [])

    def change_document(self, document, readers=None, deleted=False):
        # Exercise the sync contract of a future document mutation in the same transaction as its job.
        with self.owner.transaction():
            self.owner.execute(
                """
                UPDATE rag.documents SET readers = %s, revision = revision + 1,
                    deleted = CASE WHEN %s THEN now() END WHERE id = %s
                """,
                (readers, deleted, document["document_id"]),
            )
            self.owner.execute(
                """
                INSERT INTO rag.jobs (tenant, library, document, kind, revision, stage)
                SELECT tenant, library, id, 'sync', revision, 'accepted' FROM rag.documents WHERE id = %s
                """,
                (document["document_id"],),
            )

    def test_scheduler_roles_and_retry_limit(self):
        document = self.publish()
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            claim(self.worker, "sync")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            claim(self.sync, "ingest")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.sync.execute("SELECT * FROM rag.claim_job('sync', 4, 120)")
        with self.sync.transaction():
            set_tenant(self.sync, UUID(self.beta_id))
            self.assertEqual(self.sync.execute("SELECT id FROM chunks").fetchall(), [])

        with httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(
            lambda _: httpx.Response(503),
        )) as client:
            for _ in range(MAX_ATTEMPTS):
                lease = claim(self.sync, "sync")
                self.sync_job(document, lease=lease, index=KeywordIndex(client))
                self.owner.execute("UPDATE rag.jobs SET available = now() WHERE id = %s", (lease.job,))
        status = self.status(document).json()
        self.assertEqual((status["keyword_status"], status["keyword_attempts"]), ("failed", MAX_ATTEMPTS))
        self.assertIsNone(claim(self.sync, "sync"))
