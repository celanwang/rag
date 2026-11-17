import json
import os
import subprocess
import sys
import unittest
from uuid import UUID

import psycopg

from ingestion_fixture import IngestionFixture
from rag.db import SCHEMA_OPTIONS
from rag.jobs import claim
from rag.keyword import KeywordIndex
from rag.sync import process


class AuditTest(IngestionFixture, unittest.TestCase):
    def test_request_traces_follow_upload_readers_and_delete_into_workers(self):
        headers = {"Authorization": f"Bearer {self.writer['key']}", "X-Request-ID": "untrusted-request-header"}
        with psycopg.connect(os.environ["RAG_SYNC_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS) as sync:
            with KeywordIndex.from_env() as index, self.assertLogs("rag.audit") as log:
                upload = self.upload()
                self.assertEqual(upload.status_code, 202)
                job = upload.json()
                self.process(job)
                process(sync, claim(sync, "sync"), index)
                path = f"/v1/documents/{job['document_id']}"
                readers = self.client.patch(f"{path}/readers", json={"readers": []}, headers=headers)
                self.assertEqual(readers.status_code, 200)
                process(sync, claim(sync, "sync"), index)
                deleted = self.client.delete(path, headers=headers)
                self.assertEqual(deleted.status_code, 202)
                process(sync, claim(sync, "sync"), index)
                replay = self.client.delete(path, headers=headers)
                self.assertEqual(replay.status_code, 202)
                denied = self.client.delete(path)
                self.assertEqual(denied.status_code, 401)

        records = [json.loads(record.getMessage()) for record in log.records]
        ids = {response.headers["X-Request-ID"] for response in (upload, readers, deleted, replay, denied)}
        self.assertEqual(len(ids), 5)
        self.assertTrue(all(str(UUID(trace)) == trace for trace in ids))
        for response, events in (
            (upload, {"http.request", "document.upload", "document.publish", "document.sync"}),
            (readers, {"http.request", "document.readers", "document.sync"}),
            (deleted, {"http.request", "document.delete", "document.sync"}),
            (replay, {"http.request", "document.delete"}),
            (denied, {"http.request"}),
        ):
            trace = response.headers["X-Request-ID"]
            matches = [entry for entry in records if entry["trace_id"] == trace]
            self.assertEqual({entry["event"] for entry in matches}, events)
            for entry in matches:
                if entry["event"] == "http.request":
                    self.assertEqual(entry["status"], response.status_code)
                    continue
                self.assertEqual((entry["actor"], entry["tenant"], entry["document"]),
                                 (self.writer["id"], self.alpha_id, job["document_id"]))
        changes = [entry for entry in records if entry["event"] == "document.delete"]
        self.assertEqual([entry["outcome"] for entry in changes], ["changed", "unchanged"])
        self.assertEqual(len([entry for entry in records if entry["event"] == "document.sync"]), 3)
        self.assertNotIn(self.writer["key"], "\n".join(log.output))
        self.assertNotIn(headers["X-Request-ID"], "\n".join(log.output))

    def test_cli_authorization_changes_log_operator_and_preserve_json_stdout(self):
        for command, outcome in (("grant", "unchanged"), ("revoke", "changed"), ("revoke", "unchanged")):
            result = subprocess.run(
                [sys.executable, "-m", "rag.cli", command, self.alpha, self.writer["id"], "shop", "write"],
                capture_output=True, text=True, check=True,
            )
            receipt = json.loads(result.stdout)
            entry = json.loads(result.stderr)
            self.assertTrue(entry["actor"])
            self.assertEqual(entry["actor_type"], "operator")
            self.assertEqual(entry["outcome"], outcome)
            self.assertEqual(entry["event"], f"permission.{command}")
            self.assertEqual(entry["tenant"], self.alpha_id)
            self.assertEqual((entry["subject_app"], entry["library"], entry["permission"]),
                             (receipt["app"], receipt["library"], receipt["action"]))
            UUID(entry["trace_id"])
