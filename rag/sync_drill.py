"""Run real-process sync faults against a disposable rag-dev database, index, and broker."""
import json
import math
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import psycopg
from redis import Redis

from .benchmark import request
from .dev import ROOT, available_port, command, new_run, services, store_json, worker_commands

WORKER = ("celery", "-A", "rag.task_queue:app", "worker", "--concurrency", "1",
          "--loglevel", "INFO", "--without-gossip", "--without-mingle", "--without-heartbeat")


def wait_for(check, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.04)
    raise RuntimeError("Timed out waiting for sync drill condition")


@contextmanager
def process(env, log, *args):
    with log.open("ab") as output:
        child = subprocess.Popen((sys.executable, "-m", *args), env=env, cwd=ROOT,
                                 stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


@contextmanager
def proxy(upstream):
    reached, resume, discard = Event(), Event(), Event()
    position = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            bulk = self.path.split("?")[0].endswith("/_bulk")
            if bulk and position == "before":
                reached.set()
                if not resume.wait(9):
                    self.send_error(504)
                    return
                if discard.is_set():
                    return
            result = httpx.request(self.command, upstream + self.path, content=body, timeout=12,
                                   headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
                                   trust_env=False)
            if bulk and position == "after":
                reached.set()
                resume.wait(9)
            try:
                self.send_response(result.status_code)
                self.send_header("Content-Length", str(len(result.content)))
                self.end_headers()
                self.wfile.write(result.content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_PUT = do_POST = do_GET = do_DELETE = do_HEAD = forward

    def gate(where):
        nonlocal position
        reached.clear()
        resume.clear()
        discard.clear()
        position = where

    def release(drop=False):
        nonlocal position
        position = None
        if drop:
            discard.set()
        resume.set()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", reached, gate, release
        finally:
            resume.set()
            server.shutdown()


def run():
    with new_run("sync-drill") as (env, folder, manifest):
        name = f"rag-dev-test-redis-{manifest['run']}"
        port = available_port()
        command("docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379",
                "redis:7-alpine", "redis-server", "--appendonly", "yes", env=env, capture_output=True)
        try:
            env.update(RAG_CELERY_BROKER=f"redis://127.0.0.1:{port}/0", RAG_SYNC_TASK_TIMEOUT="8",
                       RAG_SYNC_LEASE_SECONDS="10", RAG_SYNC_EXECUTOR="pg", RAG_INGEST_EXECUTOR="pg")
            manifest["queue"] = env["RAG_CELERY_QUEUE"]
            run_cases(env, folder, name)
        finally:
            command("docker", "rm", "-fv", name, env=env, capture_output=True)


def run_cases(env, folder, redis_name):
    with proxy(env["RAG_ES_URL"]) as (es, reached, gate, release), psycopg.connect(
        env["RAG_TEST_OWNER_URL"], autocommit=True,
    ) as owner, services(env, available_port(), folder / "api.log", sync=False) as (url, children), httpx.Client(
        base_url=url, timeout=20, trust_env=False,
    ) as client:
        worker_env = env | {"RAG_ES_URL": es}
        log = folder / "queue.log"
        results = {}
        redis = Redis.from_url(env["RAG_CELERY_BROKER"], socket_timeout=1)
        wait_for(redis.ping)

        def cli(*args):
            result = command(sys.executable, "-m", "rag.cli", *args, env=env, capture_output=True, text=True)
            return json.loads(result.stdout)

        def tenant(code):
            cli("create-tenant", code, code)
            cli("create-library", code, "shop", "Shop")
            user = cli("register-app", code, "User")
            for grant in ("write", "read"):
                cli("grant", code, user["id"], "shop", grant)
            return user["key"]

        key = tenant("drill")
        outsider = tenant("drill-other")
        content = (ROOT / "docs/samples/ecommerce/alpha/returns.md").read_bytes()

        def submit(label, document=None):
            params = {"filename": "returns.md"}
            if document:
                params["document_id"] = document
            job = request(client, "POST", "/v1/knowledge-bases/shop/documents", key, params=params,
                          headers={"Idempotency-Key": label, "Content-Type": "application/octet-stream"},
                          content=content)
            wait_for(lambda: owner.execute(
                "SELECT 1 FROM rag.documents WHERE id=%s AND published=%s",
                (job["document_id"], job["version_id"]),
            ).fetchone())
            return job["document_id"]

        def jobs(document):
            return owner.execute("""SELECT id, state, attempts, revision, dispatch_after FROM rag.jobs
                WHERE document=%s AND kind='sync' ORDER BY revision""", (document,)).fetchall()

        def send(job):
            command(sys.executable, "-c", "import sys; from uuid import UUID; from rag.task_queue import send; "
                    "send(UUID(sys.argv[1]))", str(job), env=worker_env)

        def synced(document):
            return jobs(document)[-1][1] == "done"

        def search(reader):
            return request(client, "POST", "/v1/retrieve", reader, json={
                "knowledge_base_code": "shop", "question": "RET-ALPHA-07", "mode": "keyword", "top_k": 30,
            })["results"]

        with process(worker_env, log, *WORKER):
            doc = submit("old")
            old = jobs(doc)[0][0]
            submit("new", doc)
            new = jobs(doc)[-1][0]
            for job in (new, old, new):
                send(job)
            wait_for(lambda: all(row[1] == "done" for row in jobs(doc)))
            assert [row[2] for row in jobs(doc)] == [1, 1]
            assert any(item["document_id"] == doc for item in search(key))
            assert not search(outsider)
            results["duplicate_and_out_of_order"] = [row[2:4] for row in jobs(doc)]
            cutoff = submit("reserve-without-send")
            command(sys.executable, "-c", "from rag.task_queue import connect, reserve; "
                    "conn=connect(); assert reserve(conn)", env=worker_env)
            with process(worker_env, log, "rag.task_queue"):
                wait_for(lambda: synced(cutoff))
            results["reserved_without_publishing"] = "recovered"

        with process(worker_env, log, "rag.task_queue"):
            for where in ("before", "after"):
                with process(worker_env, log, *WORKER) as child:
                    gate(where)
                    document = submit(f"kill-{where}")
                    assert reached.wait(12)
                    crash = time.monotonic()
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    release(drop=where == "before")
                with process(worker_env, log, *WORKER):
                    wait_for(lambda: synced(document))
                results[f"worker_kill_{where}_bulk"] = {
                    "attempts": jobs(document)[-1][2], "recovery_s": time.monotonic() - crash,
                }

            with process(worker_env, log, *WORKER):
                command("docker", "stop", "-t", "1", redis_name, env=env, capture_output=True)
                backlog = [submit(f"outage-{i}") for i in range(4)]
                assert all(jobs(document)[-1][1] == "queued" for document in backlog)
                restored = time.monotonic()
                command("docker", "start", redis_name, env=env, capture_output=True)
                wait_for(lambda: all(synced(document) for document in backlog))
                results["broker_outage"] = {"drained": len(backlog), "recovery_s": time.monotonic() - restored}

        fallback = submit("fallback")
        with process(worker_env, log, "rag.sync"):
            wait_for(lambda: synced(fallback))
        results["fallback_to_pg"] = {"attempts": jobs(fallback)[-1][2]}

        for mode in ("pg", "celery"):
            with ExitStack() as stack:
                if mode == "pg":
                    stack.enter_context(process(worker_env, log, "rag.sync"))
                else:
                    stack.enter_context(process(worker_env, log, "rag.task_queue"))
                    stack.enter_context(process(worker_env, log, *WORKER))
                    wait_for(lambda: "ready." in log.read_text().split("Connected to redis")[-1], timeout=10)
                started = time.monotonic()
                samples = []
                for number in range(6):
                    document = submit(f"perf-{mode}-{number}")
                    published = time.monotonic()
                    wait_for(lambda: jobs(document)[-1][1] != "queued")
                    claimed = time.monotonic()
                    wait_for(lambda: synced(document))
                    samples.append({"queue_ms": (claimed - published) * 1000,
                                    "publish_to_searchable_ms": (time.monotonic() - published) * 1000})
                elapsed = time.monotonic() - started
                results[mode] = {
                    "documents": len(samples), "documents_per_second": len(samples) / elapsed,
                    "queue_p95_ms": sorted(s["queue_ms"] for s in samples)[math.ceil(len(samples) * 0.95) - 1],
                    "samples": samples,
                }

        for action in ("publish", "delete", "revoke"):
            with process(worker_env, log, "rag.sync"):
                gate("before")
                document = submit(f"slow-{action}")
                assert reached.wait(5)
                began = time.monotonic()
                with ThreadPoolExecutor(1) as executor:
                    headers = {"Authorization": f"Bearer {key}"}
                    if action == "publish":
                        operation = executor.submit(submit, "slow-2", document)
                    else:
                        operation = executor.submit(client.request, "DELETE" if action == "delete" else "PATCH",
                                                    f"/v1/documents/{document}" + ("/readers" if action == "revoke" else ""),
                                                    headers=headers, **({"json": {"readers": []}} if action == "revoke" else {}))
                    time.sleep(1)
                    blocked = not operation.done()
                    release()
                    response = operation.result(timeout=15)
                results[f"slow_es_{action}"] = {"wait_s": time.monotonic() - began, "blocked_while_held": blocked,
                                                "status": response.status_code if isinstance(response, httpx.Response) else 200}
                wait_for(lambda: synced(document))
        children[1].terminate()
        children[1].wait(timeout=15)
        ingest_env = worker_env | {"RAG_INGEST_EXECUTOR": "celery"}
        with ExitStack() as stack:
            for args in worker_commands(ingest_env, "ingest"):
                stack.enter_context(process(ingest_env, log, *args))
            stack.enter_context(process(worker_env, log, "rag.sync"))
            document = submit("celery-ingestion")
            wait_for(lambda: synced(document))
            previous = owner.execute("SELECT published FROM rag.documents WHERE id=%s", (document,)).fetchone()[0]
            bad = request(client, "POST", "/v1/knowledge-bases/shop/documents", key,
                          params={"filename": "returns.md", "document_id": document},
                          headers={"Idempotency-Key": "celery-invalid", "Content-Type": "application/octet-stream"},
                          content=b"<!-- rag-policy bad-json -->\n# Invalid\n\nInvalid metadata.")

            def failed():
                status = request(client, "GET", f"/v1/ingestion-jobs/{bad['job_id']}", key)
                return status if status["state"] == "failed" else None

            status = wait_for(failed)
            assert status["error"] == "content_invalid"
            assert status["published_version_id"] == str(previous)
            assert any(item["document_id"] == document for item in search(key))
            results["celery_ingest_preserves_published"] = {"state": status["state"], "error": status["error"]}
        store_json(folder / "acceptance.json", results)
