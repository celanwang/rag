import os
import unittest

import psycopg

from ingestion_fixture import SAMPLES, IngestionFixture
from rag.db import SCHEMA_OPTIONS
from rag.jobs import claim
from rag.keyword import KeywordIndex
from rag.retention import retain
from rag.sync import process


class RetentionTest(IngestionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.maintenance = self.enterContext(psycopg.connect(
            os.environ["RAG_TEST_OWNER_URL"], autocommit=True, options=SCHEMA_OPTIONS,
        ))

    def clean(self, *, apply=True):
        return retain(self.maintenance, self.alpha, days=30, limit=100, apply=apply)

    def age(self, *jobs):
        self.owner.execute("""
            UPDATE rag.versions SET retired = now() - interval '31 days' WHERE id = ANY(%s::uuid[])
        """, ([job["version_id"] for job in jobs],))

    def test_prunes_only_expired_terminal_versions_and_preserves_receipts(self):
        first = self.accepted()
        self.process(first)
        document = first["document_id"]
        second = self.accepted(document=document, request="second")
        self.process(second)
        stale = self.accepted(document=document, request="stale")
        stale_lease = claim(self.worker, "ingest")
        target = self.accepted(document=document, request="target")
        self.assertEqual(str(claim(self.worker, "ingest").job), target["job_id"])
        beta = self.accepted(file="beta/returns.md", key=self.beta_writer["key"])
        self.process(beta)
        other = self.accepted(document=beta["document_id"], request="other", file="beta/returns.md",
                              key=self.beta_writer["key"])
        self.process(other)

        retired = self.owner.execute("SELECT retired FROM rag.versions WHERE id = %s", (first["version_id"],)).fetchone()[0]
        self.assertIsNotNone(retired)
        self.assertEqual(self.clean(apply=False)["versions"], 0)
        self.age(first, second, stale, target, beta)
        preview = self.clean(apply=False)
        self.assertEqual(preview["versions"], 1)
        self.assertGreater(preview["chunks"], 0)
        self.assertEqual(len(self.originals()), 4)
        with self.owner.transaction():
            self.owner.execute("SELECT id FROM rag.documents WHERE id = %s FOR UPDATE", (document,))
            self.assertEqual(self.clean()["versions"], 0)
        applied = self.clean()
        self.assertEqual((applied["versions"], applied["chunks"]), (1, preview["chunks"]))
        self.assertEqual(self.clean()["versions"], 0)

        ids = self.owner.execute("SELECT id::text FROM rag.versions").fetchall()
        remaining = {row[0] for row in ids}
        self.assertNotIn(first["version_id"], remaining)
        self.assertTrue({job["version_id"] for job in (second, stale, target, beta, other)} <= remaining)
        self.assertEqual(self.accepted(), first)
        self.assertEqual(self.upload(body=b"different original").status_code, 409)
        receipt = self.status(first).json()
        self.assertEqual((receipt["version_id"], receipt["published_version_id"]),
                         (first["version_id"], second["version_id"]))
        download = self.client.get(f"/v1/documents/{document}/file", headers={"Authorization": f"Bearer {self.reader}"})
        self.assertEqual(download.content, (SAMPLES / "alpha/returns.md").read_bytes())
        self.process(stale, lease=stale_lease)
        self.assertEqual(self.clean()["versions"], 1)

    def test_deleted_original_waits_for_es_and_leaves_idempotent_tombstone(self):
        job = self.accepted()
        self.process(job)
        document = job["document_id"]
        headers = {"Authorization": f"Bearer {self.writer['key']}"}
        self.assertEqual(self.client.delete(f"/v1/documents/{document}", headers=headers).status_code, 202)
        self.owner.execute("UPDATE rag.documents SET deleted = now() - interval '31 days' WHERE id = %s", (document,))
        self.assertEqual(self.clean()["versions"], 0)
        with psycopg.connect(os.environ["RAG_SYNC_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS) as sync:
            with KeywordIndex.from_env() as index:
                for _ in range(2):
                    process(sync, claim(sync, "sync"), index)
        self.assertEqual(self.clean()["versions"], 1)
        self.assertEqual(self.originals(), [])
        self.assertEqual(self.accepted(), job)
        self.assertEqual(self.status(job).json()["keyword_status"], "synced")
        self.assertEqual(self.owner.execute(
            "SELECT target, published FROM rag.documents WHERE id = %s", (document,),
        ).fetchone(), (None, None))
        self.assertEqual(self.upload(document=document, request="new-after-delete").status_code, 404)
