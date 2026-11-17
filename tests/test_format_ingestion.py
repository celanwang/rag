import hashlib
import json
import os
import unittest
from dataclasses import asdict
from unittest.mock import patch

import httpx
import psycopg

from ingestion_fixture import IngestionFixture
from rag.api import app
from rag.db import SCHEMA_OPTIONS
from rag.format_parser import DOCX, PDF, DOCX_EXPANDED_BYTES, parse_binary
from rag.generation import AnswerModel
from rag.jobs import claim
from rag.keyword import KeywordIndex
from rag.models import BailianModels
from rag.parse import version_config
from rag.sync import process as synchronize
from rag.text import MAX_UPLOAD_BYTES
from rag.worker import process
from test_authorization import cli
from test_formats import CLAUSE, TABLE_CLAUSE, docx_variant, generic_zip, pdf_variant, sample


RETURNS_QUESTION = "鸢尾门店退货须在签收之后几天内申请？"
RETURNS_POLICY = {
    "id": "iris-returns", "rule": "returns", "scope": "iris", "scope_label": "鸢尾门店",
    "authority": 1, "value": "14天", "effective_from": "2026-09-01", "effective_to": "2026-10-01",
    "topic_labels": ["退货"],
}


def policy_markdown(heading, clause, policy):
    return (f"<!-- rag-policy {json.dumps({heading: policy}, ensure_ascii=False)} -->\n"
            f"# {heading}\n\n{clause}\n").encode()


class FormatIngestionTest(IngestionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sync = self.enterContext(psycopg.connect(
            os.environ["RAG_SYNC_DATABASE_URL"], autocommit=True, options=SCHEMA_OPTIONS,
        ))
        self.index = self.enterContext(KeywordIndex.from_env())
        if os.environ.get("RAG_FORMAT_LIVE_MODELS") == "1":
            self.live = self.enterContext(BailianModels.from_env())
            models = self.live
        else:
            self.live = None
            client = self.enterContext(httpx.Client(base_url="https://models.invalid/", transport=httpx.MockTransport(self.model)))
            models = BailianModels(client)
        self.enterContext(patch.object(app.state.retriever, "models", models))

    @staticmethod
    def model(request):
        data = json.loads(request.content)
        if data["model"] == "qwen3-rerank":
            rows = [{"index": i, "relevance_score": float(CLAUSE in text)}
                    for i, text in enumerate(data["input"]["documents"])]
            return httpx.Response(200, json={"output": {"results": rows}})
        return httpx.Response(200, json={"output": {"embeddings": [{"text_index": 0, "embedding": [0.25] * 512}]}})

    @staticmethod
    def headers(key):
        return {"Authorization": f"Bearer {key}"}

    def ingest(self, filename, *, request=None, document=None, body=None, key=None, policy=None):
        return self.accepted(request=request or filename, file=filename, document=document, key=key,
                             body=sample(filename) if body is None else body, policy=policy)

    def publish(self, job, *, sync=True):
        if self.live is None:
            self.process(job)
        else:
            lease = claim(self.worker, "ingest")
            self.assertEqual(str(lease.job), job["job_id"])
            process(self.worker, lease, self.live)
        state = self.status(job).raise_for_status().json()
        self.assertEqual((state["state"], state["published_version_id"]), ("done", job["version_id"]), state)
        if sync:
            self.sync_job(job)
            self.assertEqual(self.status(job).raise_for_status().json()["keyword_status"], "synced")
        return state

    def sync_job(self, job):
        lease = claim(self.sync, "sync")
        self.assertIsNotNone(lease)
        document = self.owner.execute("SELECT document::text FROM rag.jobs WHERE id = %s", (lease.job,)).fetchone()[0]
        self.assertEqual(document, job["document_id"])
        synchronize(self.sync, lease, self.index)

    def retrieve(self, mode, question="鸢尾门店退货 Iris returns 14 days", key=None):
        response = self.client.post("/v1/retrieve", headers=self.headers(key or self.reader),
                                    json={"knowledge_base_code": "shop", "mode": mode, "question": question, "top_k": 10})
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()
        self.assertFalse(results["degraded"], results)
        return results["results"]

    def cite(self, identifier, key=None):
        return self.client.get(f"/v1/citations/{identifier}", headers=self.headers(key or self.reader))

    def download(self, document, version=None, key=None):
        params = {"version_id": version} if version else {}
        return self.client.get(f"/v1/documents/{document}/file", params=params, headers=self.headers(key or self.reader))

    def assert_clause(self, job, expected, needle=CLAUSE, question="鸢尾门店退货 Iris returns 14 days"):
        evidence = None
        for mode in ("vector", "keyword"):
            hits = self.retrieve(mode, question)
            self.assertEqual({hit["version_id"] for hit in hits}, {job["version_id"]}, (mode, hits))
            evidence = next((hit for hit in hits if needle in hit["body"]), None)
            self.assertIsNotNone(evidence, (mode, hits))
            self.assertEqual(evidence["location"], expected)
            self.assertEqual(self.cite(evidence["chunk_id"]).raise_for_status().json(), evidence)
            self.assertEqual(evidence["line_start"], expected.get("line_start"))
            self.assertEqual(evidence["line_end"], expected.get("line_end"))
        return evidence

    def answer(self, question=RETURNS_QUESTION, **params):
        def chat(request):
            payload = json.loads(request.content)
            prompt = json.loads(payload["messages"][1]["content"])
            row = next(row for row in prompt["evidence"] if CLAUSE in row["excerpts"])
            draft = {"status": "answered", "selections": [{"evidence_id": row["evidence_id"],
                                                         "excerpt_index": row["excerpts"].index(CLAUSE)}]}
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(draft)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
            })

        with httpx.Client(base_url="https://generate.invalid/", transport=httpx.MockTransport(chat)) as model, \
             patch.object(app.state.answerer, "generator", AnswerModel(model, "fake")):
            return self.client.post("/v1/answer", headers=self.headers(self.reader), json={
                "knowledge_base_code": "shop", "question": question, **params,
            }).raise_for_status().json()

    def assert_answer(self, expected, **params):
        result = self.answer(**params)
        self.assertEqual(result["status"], "answered", result)
        citation, = result["citations"]
        self.assertEqual(citation["location"], expected)
        self.assertEqual(citation["line_start"], expected.get("line_start"))
        self.assertEqual(self.cite(citation["chunk_id"]).raise_for_status().json()["location"], expected)
        return citation

    def test_one_document_three_versions_in_both_indexes_and_answer(self):
        section = ["门店制度 / Store rules", "退货条款 / Returns"]
        cases = (
            ("returns.md", "text/markdown", {"kind": "lines", "line_start": 3, "line_end": 3}),
            ("returns.docx", DOCX, {"kind": "docx_paragraph", "section": section, "paragraph": 3}),
            ("returns.pdf", PDF, {"kind": "pdf_page", "page": 2}),
        )
        document = previous = None
        for filename, mime, location in cases:
            with self.subTest(filename=filename):
                job = self.ingest(filename, document=document)
                self.assertEqual(job, self.ingest(filename, document=document))
                self.assertEqual(self.upload(file=filename, request=filename, document=document,
                                             body=sample(filename) + b" ").status_code, 409)
                document = job["document_id"]
                self.assertEqual(self.status(job).json()["state"], "queued")
                if previous:
                    self.assertEqual(self.cite(previous["chunk_id"]).status_code, 200)
                    self.assertEqual(self.download(document, previous["version_id"]).status_code, 200)
                    for mode in ("vector", "keyword"):
                        self.assertEqual({hit["version_id"] for hit in self.retrieve(mode)}, {previous["version_id"]})
                self.publish(job, sync=False)
                if previous:
                    self.assertEqual(self.cite(previous["chunk_id"]).status_code, 404)
                    self.assertEqual(self.download(document, previous["version_id"]).status_code, 404)
                    self.assertEqual(self.retrieve("keyword"), [])
                self.sync_job(job)
                self.assertEqual(self.status(job).json()["keyword_status"], "synced")
                previous = self.assert_clause(job, location)
                self.assert_answer(location)

                if mime == DOCX:
                    self.assert_clause(job, {"kind": "docx_table_row", "section": section, "table": 1, "row": 2},
                                       TABLE_CLAUSE, "ORCHID731")
                original = self.download(document, job["version_id"]).raise_for_status()
                self.assertEqual(original.headers["content-type"].split(";")[0], mime)
                self.assertEqual(original.content, sample(filename))
                if mime == "text/markdown":
                    self.assertIn(previous["body"], original.content.decode().splitlines()[location["line_start"] - 1])
                else:
                    blocks = parse_binary(mime, original.content).blocks
                    cited_block = next(block for block in blocks
                                       if json.loads(json.dumps(asdict(block.location))) == location)
                    self.assertIn(previous["body"], cited_block.text)
                stored = self.owner.execute("SELECT checksum, config FROM rag.versions WHERE id = %s",
                                            (job["version_id"],)).fetchone()
                self.assertEqual(stored, (hashlib.sha256(sample(filename)).digest(), version_config(mime)))

    def test_dated_policy_across_formats_mixed_pdf_and_revocation(self):
        section = ["门店制度 / Store rules", "退货条款 / Returns"]
        cases = (
            ("returns.md", policy_markdown("退货条款 / Returns", CLAUSE, RETURNS_POLICY),
             {"kind": "lines", "line_start": 4, "line_end": 4}),
            ("returns.docx", sample("returns.docx"),
             {"kind": "docx_paragraph", "section": section, "paragraph": 3}),
            ("overlay.pdf", pdf_variant(with_image=True), {"kind": "pdf_page", "page": 1}),
        )
        document = previous = None
        for filename, original, location in cases:
            with self.subTest(filename=filename):
                job = self.ingest(filename, document=document, body=original)
                document = job["document_id"]
                report = self.publish(job)["parse_report"]
                if filename.endswith(".pdf"):
                    self.assertEqual((report["status"], report["mixed_image_pages"], report["scan_pages"]),
                                     ("partial", [1], []))
                    self.assertIn("pdf_mixed_page_images_not_extracted", report["warnings"])
                    self.assertNotIn("replacement terms are pixels", parse_binary(PDF, original).blocks[0].text)
                elif filename.endswith(".docx"):
                    self.assertEqual(report["status"], "complete")
                if previous:
                    self.assertEqual(self.cite(previous["chunk_id"]).status_code, 404)
                    self.assertEqual(self.download(document, previous["version_id"]).status_code, 404)
                hit = self.assert_clause(job, location)
                self.assertEqual(hit["policy_data"]["effective_from"], RETURNS_POLICY["effective_from"])
                previous = self.assert_answer(location, as_of="2026-09-01")
                self.assertEqual(previous["version_id"], job["version_id"])
                self.assertEqual(self.download(document, job["version_id"]).raise_for_status().content, original)
                for day in ("2026-08-31", "2026-10-01"):
                    result = self.answer(as_of=day)
                    self.assertEqual((result["status"], result["reason"], result["citations"]),
                                     ("insufficient", "policy_not_available_on_date", []))

        self.client.patch(f"/v1/documents/{document}/readers", headers=self.headers(self.writer["key"]),
                          json={"readers": []}).raise_for_status()
        for synced in (False, True):
            if synced:
                self.sync_job(job)
            for mode in ("vector", "keyword"):
                self.assertEqual(self.retrieve(mode), [])
            result = self.answer(as_of="2026-09-01")
            self.assertEqual((result["status"], result["citations"]), ("insufficient", []))
            self.assertEqual(self.cite(previous["chunk_id"]).status_code, 404)
            self.assertEqual(self.download(document, previous["version_id"]).status_code, 404)

    def test_changed_binary_policy_needs_confirmation(self):
        old = self.ingest("returns.md", body=policy_markdown("退货条款", "鸢尾门店退货须在签收后七天内申请。",
                                                            RETURNS_POLICY | {"value": "7天"}))
        self.publish(old)
        document = old["document_id"]
        proposed = self.ingest("returns.pdf", request="auto", document=document)
        self.process(proposed)
        status = self.status(proposed).raise_for_status().json()
        self.assertEqual((status["state"], status["error"], status["published_version_id"]),
                         ("failed", "policy_confirmation_required", old["version_id"]))
        self.assertEqual(self.download(document).raise_for_status().content, self.download(document, old["version_id"]).content)

        confirmed = self.ingest("returns.pdf", request="confirmed", document=document, policy=RETURNS_POLICY)
        self.assertEqual(confirmed, self.ingest("returns.pdf", request="confirmed", document=document,
                                                policy=RETURNS_POLICY))
        conflict = self.upload(file="returns.pdf", request="confirmed", document=document, body=sample("returns.pdf"),
                               policy=RETURNS_POLICY | {"effective_from": "2026-09-02"})
        self.assertEqual(conflict.status_code, 409)
        self.publish(confirmed)
        citation = self.assert_answer({"kind": "pdf_page", "page": 2}, as_of="2026-09-01")
        self.assertEqual(citation["version_id"], confirmed["version_id"])

    def test_unrelated_policy_conflict_does_not_preempt_an_answer(self):
        returns = self.ingest("returns.pdf")
        self.publish(returns)
        self.assert_answer({"kind": "pdf_page", "page": 2})
        filenames = []
        for key, value in (("a", "24小时"), ("b", "48小时")):
            name = f"oncall-{key}.md"
            filenames.append(name)
            policy = RETURNS_POLICY | {"id": f"oncall-{key}", "rule": "oncall-transfer", "value": value,
                                       "topic_labels": ["换班"], "effective_from": "2020-01-01", "effective_to": None}
            self.publish(self.ingest(name, body=policy_markdown(f"值班制度 {key}", f"值班换班须提前{value}报备。", policy)))

        self.assert_answer({"kind": "pdf_page", "page": 2})
        result = self.answer("鸢尾门店值班换班要提前多久报备？", as_of="2026-09-01")
        self.assertEqual(result["status"], "policy_conflict")
        self.assertCountEqual([citation["filename"] for citation in result["citations"]], filenames)

    def test_partial_pdf_failed_replacement_and_read_scope(self):
        job = self.ingest("mixed.pdf")
        report = self.publish(job)["parse_report"]
        self.assertEqual((report["status"], report["scan_pages"], report["page_count"]), ("partial", [1, 3], 3))
        location = {"kind": "pdf_page", "page": 2}
        evidence = self.assert_clause(job, location)
        failed = self.ingest("scan.pdf", document=job["document_id"], body=pdf_variant(scanned=True))
        self.process(failed)
        state = self.status(failed).json()
        self.assertEqual((state["state"], state["error"], state["published_version_id"]),
                         ("failed", "pdf_scanned", job["version_id"]))
        self.assertEqual(state["parse_report"]["scan_pages"], [1])
        self.assert_clause(job, location)
        self.assertEqual(self.download(job["document_id"]).content, sample("mixed.pdf"))
        self.assertEqual(self.download(job["document_id"], failed["version_id"]).status_code, 404)

        beta = cli("register-app", self.beta, "Reader")
        cli("grant", self.beta, beta["id"], "shop", "read")
        for mode in ("vector", "keyword"):
            self.assertEqual(self.retrieve(mode, key=beta["key"]), [])
        self.assertEqual(self.cite(evidence["chunk_id"], beta["key"]).status_code, 404)
        self.assertEqual(self.download(job["document_id"], job["version_id"], beta["key"]).status_code, 404)
        self.assertEqual(self.status(job, key=self.beta_writer["key"]).status_code, 404)

        self.client.patch(f"/v1/documents/{job['document_id']}/readers", headers=self.headers(self.writer["key"]),
                          json={"readers": []}).raise_for_status()
        for synced in (False, True):
            if synced:
                self.sync_job(job)
            for mode in ("vector", "keyword"):
                self.assertEqual(self.retrieve(mode), [])
            self.assertEqual(self.cite(evidence["chunk_id"]).status_code, 404)
            self.assertEqual(self.download(job["document_id"], job["version_id"]).status_code, 404)

    def test_spoofed_corrupt_encrypted_and_oversized_inputs(self):
        for filename, body in (("spoof.pdf", b"plain text"), ("spoof.docx", sample("returns.pdf")),
                               ("spoof.txt", sample("returns.pdf")), ("legacy.doc", sample("legacy.doc"))):
            response = self.upload(file=filename, body=body)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.upload(file="over.pdf", body=b"%PDF-" + b"x" * MAX_UPLOAD_BYTES).status_code, 413)
        self.assertEqual(self.originals(), [])
        for filename, body, code in (
            ("corrupt.pdf", b"%PDF-1.4\nnot a valid PDF\n", "content_corrupt"),
            ("encrypted.pdf", sample("encrypted.pdf"), "content_encrypted"),
            ("encrypted.docx", sample("encrypted.docx"), "content_encrypted"),
            ("spoof.docx", generic_zip(), "format_mismatch"),
            ("expanded.docx", docx_variant(extra=b"x" * (DOCX_EXPANDED_BYTES + 1)), "content_limit"),
        ):
            with self.subTest(filename=filename):
                job = self.ingest(filename, body=body)
                self.process(job)
                state = self.status(job).raise_for_status().json()
                self.assertEqual((state["state"], state["error"], state["published_version_id"]),
                                 ("failed", code, None))
                self.assertIsNone(claim(self.sync, "sync"))
