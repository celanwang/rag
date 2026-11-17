import argparse
import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import httpx
import psycopg

from .cli import ConfigurationError, short_code
from .db import SCHEMA_OPTIONS
from .retention import retain

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docs/env/rag/compose.yaml"
ENV_FILE = Path(os.getenv("RAG_DEV_ENV_FILE", str(COMPOSE_FILE.with_name(".env")))).expanduser().resolve()
COMPOSE_PROJECT = os.getenv("RAG_DEV_PROJECT", "rag-dev")
COMPOSE = (
    "docker", "compose", "-p", COMPOSE_PROJECT, "--env-file", str(ENV_FILE),
    "-f", str(COMPOSE_FILE),
)
RUNS = ROOT / ".rag/dev"
SCHEMA_FILE = "/docker-entrypoint-initdb.d/schema.sql"
SCHEMA_VERSION = "rag schema version 5"
SCHEMA_UPGRADES = {"rag schema version 1": "002_sync_dispatch.sql",
                   "rag schema version 2": "003_ingest_dispatch.sql",
                   "rag schema version 3": "004_index_generations.sql",
                   "rag schema version 4": "005_ingest_parse_report.sql"}
MIGRATIONS = ROOT / "docs/env/rag/migrations"
SECRETS = (
    "RAG_POSTGRES_PASSWORD", "RAG_API_PASSWORD", "RAG_ADMIN_PASSWORD", "RAG_WORKER_PASSWORD",
    "RAG_SYNC_PASSWORD",
)


def command(*args: str, env: dict, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=env, check=True, **kwargs)


def read_local_env() -> dict:
    values = {}
    for line in ENV_FILE.read_text().splitlines() if ENV_FILE.is_file() else ():
        name, separator, raw = line.strip().removeprefix("export ").partition("=")
        if separator and not name.startswith("#") and name.strip():
            values[name.strip()] = " ".join(shlex.split(raw, comments=True))
    return values


def container_env(name: str) -> dict:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", name],
        capture_output=True, text=True, check=False,
    )
    return dict(entry.split("=", 1) for entry in json.loads(result.stdout)) if result.returncode == 0 else {}


def configure() -> None:
    values = read_local_env()
    postgres = container_env(f"{COMPOSE_PROJECT}-postgres-1")
    recorded = postgres.copy()
    recorded["RAG_POSTGRES_PASSWORD"] = postgres.get("POSTGRES_PASSWORD")
    if not postgres and not all(values.get(name) for name in SECRETS[:3]):
        volume = subprocess.run(
            ["docker", "volume", "inspect", f"{COMPOSE_PROJECT}_postgres-data"], capture_output=True, check=False,
        )
        if volume.returncode == 0:
            raise SystemExit(f"Restore the PostgreSQL, API, and admin passwords in {ENV_FILE} for the existing volume.")
    missing = {name: recorded.get(name) or secrets.token_hex(24) for name in SECRETS if not values.get(name)}
    if missing:
        with os.fdopen(os.open(ENV_FILE, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600), "a") as stream:
            for name, value in missing.items():
                stream.write(f"\n{name}={shlex.quote(value)}\n")


def runtime(database: str = "rag") -> dict:
    local = read_local_env()
    if any(not local.get(name) for name in SECRETS):
        raise SystemExit("Run python -m rag.dev up first.")
    env = os.environ | local
    for key, role, password in (
        ("RAG_DATABASE_URL", "rag_api", "RAG_API_PASSWORD"),
        ("RAG_ADMIN_DATABASE_URL", "rag_admin", "RAG_ADMIN_PASSWORD"),
        ("RAG_WORKER_DATABASE_URL", "rag_worker", "RAG_WORKER_PASSWORD"),
        ("RAG_SYNC_DATABASE_URL", "rag_sync", "RAG_SYNC_PASSWORD"),
        ("RAG_TEST_OWNER_URL", "postgres", "RAG_POSTGRES_PASSWORD"),
    ):
        port = env.get("RAG_POSTGRES_PORT", "55433")
        env[key] = f"postgresql://{role}:{quote(env[password], safe='')}@127.0.0.1:{port}/{database}"
    env["RAG_ES_URL"] = f"http://127.0.0.1:{env.get('RAG_ES_PORT', '59201')}"
    env.setdefault("RAG_ES_INDEX", "rag-chunks-v1")
    env.setdefault("RAG_CELERY_BROKER", f"redis://127.0.0.1:{env.get('RAG_REDIS_PORT', '56379')}/0")
    env.setdefault("RAG_CELERY_QUEUE", f"{database}-sync")
    env.setdefault("RAG_CELERY_INGEST_QUEUE", f"{database}-ingest")
    return env


def psql(env: dict, database: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return command(
        *COMPOSE, "exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", database, *args, env=env, **kwargs,
    )


def upgrade_schema(env: dict, database: str) -> None:
    query = """
        SELECT CASE WHEN ns IS NULL THEN 'empty'
                    ELSE COALESCE(obj_description(ns, 'pg_namespace'), 'unversioned') END
        FROM to_regnamespace('rag') AS ns
    """
    version = psql(env, database, "-Atc", query, capture_output=True, text=True).stdout.strip()
    if version == "empty":
        psql(env, database, "-qf", SCHEMA_FILE, "-v", "reuse_roles=on")
        version = "rag schema version 1"
    while version in SCHEMA_UPGRADES:
        psql(env, database, "-qf", "-", input=(MIGRATIONS / SCHEMA_UPGRADES[version]).read_text(), text=True)
        updated = psql(env, database, "-Atc", query, capture_output=True, text=True).stdout.strip()
        if version == updated:
            raise RuntimeError("Database migration did not advance schema version")
        version = updated
    if version != SCHEMA_VERSION:
        raise SystemExit(f"Unsupported RAG database schema version: {version}")


def up() -> None:
    configure()
    env = runtime()
    profile = ("--profile", "celery") if "celery" in (
        env.get("RAG_SYNC_EXECUTOR"), env.get("RAG_INGEST_EXECUTOR"),
    ) else ()
    command(*COMPOSE, *profile, "up", "-d", "--wait", "--wait-timeout", "90", env=env)
    upgrade_schema(env, "rag")
    command(*COMPOSE, "ps", env=env)


def store_json(path: Path, data: dict) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    Path(stream.name).replace(path)


@contextmanager
def new_run(kind: str):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    folder = RUNS / f"{kind}-{stamp}"
    database = "rag" if kind == "demo" else f"rag_{kind}_{stamp.replace('-', '_')}"
    env = runtime(database)
    folder.mkdir(parents=True, mode=0o700)
    if kind != "demo":
        env["RAG_ES_INDEX"] = f"rag-dev-{kind}-{stamp}"
        env["RAG_CELERY_QUEUE"] = f"{env['RAG_ES_INDEX']}-sync"
        env["RAG_CELERY_INGEST_QUEUE"] = f"{env['RAG_ES_INDEX']}-ingest"
    manifest = {
        "kind": kind, "run": stamp, "status": "running", "database": database,
        "index": env["RAG_ES_INDEX"],
    }
    store_json(folder / "run.json", manifest)
    print(f"{kind}: database={database}, index={manifest['index']}", flush=True)
    print(folder, flush=True)
    try:
        if kind != "demo":
            command(*COMPOSE, "exec", "-T", "postgres", "createdb", "-U", "postgres", database, env=env)
            upgrade_schema(env, database)
        yield env, folder, manifest
    except BaseException:
        manifest["status"] = "failed"
        raise
    else:
        manifest["status"] = "passed"
    finally:
        store_json(folder / "run.json", manifest)


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def worker_commands(env: dict, kind: str) -> tuple[tuple[str, ...], ...]:
    executor = env.get(f"RAG_{kind.upper()}_EXECUTOR", "pg")
    if executor == "pg":
        return (("rag.sync" if kind == "sync" else "rag.worker",),)
    if executor != "celery":
        raise ValueError(f"{kind} executor must be pg or celery")
    queue = env["RAG_CELERY_QUEUE" if kind == "sync" else "RAG_CELERY_INGEST_QUEUE"]
    return (
        ("rag.task_queue", "--kind", kind),
        ("celery", "-A", "rag.task_queue:app", "worker", "-Q", queue, "--hostname", f"{queue}@%h",
         "--concurrency", "1", "--without-gossip", "--without-mingle", "--without-heartbeat"),
    )


@contextmanager
def services(env: dict, port: int, log: Path | None = None, *, sync: bool = True):
    if not env.get("DASHSCOPE_API_KEY"):
        raise SystemExit("Set DASHSCOPE_API_KEY or pass --env-file .env to uv.")
    stream = log.open("ab") if log else None
    processes = []
    try:
        for args in (
            ("uvicorn", "rag.api:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"),
            *worker_commands(env, "ingest"), *(worker_commands(env, "sync") if sync else ()),
        ):
            processes.append(subprocess.Popen(
                (sys.executable, "-m", *args), env=env, cwd=ROOT, stdout=stream,
                stderr=subprocess.STDOUT if stream else None,
            ))
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=url, timeout=3, trust_env=False) as client:
            for _ in range(80):
                if any(proc.poll() is not None for proc in processes):
                    raise RuntimeError(f"A RAG service exited. Log: {log}")
                try:
                    if client.get("/openapi.json").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(f"The API did not start. Log: {log}")
        yield url, processes
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if stream:
            stream.close()


def test() -> None:
    with new_run("test") as (env, folder, _):
        args = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
        with (folder / "test.log").open("w") as output, subprocess.Popen(
            args, env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ) as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                output.write(line)
                output.flush()
            if process.wait() != 0:
                raise SystemExit(process.returncode)


def benchmark(repeat: int, *, bad_cases: bool = False, paired: bool = False, evidence: bool = False) -> None:
    with new_run("benchmark") as (env, folder, _), services(env, available_port(), folder / "services.log") as (url, _):
        command(
            sys.executable, "-m", "rag.benchmark", "--url", url, "--repeat", str(repeat),
            "--out", str(folder / "benchmark.json"), *(["--bad-cases"] if bad_cases else []),
            *(["--paired"] if paired else []), *(["--evidence"] if evidence else []), env=env,
        )


def answer_eval(split: str, paired: bool, repeat: int) -> None:
    with new_run("answer-eval") as (env, folder, manifest):
        manifest["split"] = split
        manifest["paired"] = paired
        with services(env, available_port(), folder / "services.log") as (url, _):
            command(sys.executable, "-m", "rag.answer_eval", "--url", url, "--split", split,
                    "--repeat", str(repeat), "--out", str(folder / "answers.json"),
                    *(["--paired"] if paired else []), env=env)


def demo() -> None:
    with new_run("demo") as (env, folder, manifest), services(env, available_port(), folder / "services.log") as (url, _):
        def cli(*args: str) -> dict:
            with (folder / "services.log").open("ab") as log:
                return json.loads(command(sys.executable, "-m", "rag.cli", *args, env=env,
                                          stdout=subprocess.PIPE, stderr=log).stdout)

        samples = ROOT / "docs/samples/ecommerce"
        manifest["tenants"] = {}
        readers = {}
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            for group in ("alpha", "beta"):
                code = f"ecommerce-{group}-{manifest['run']}"
                cli("create-tenant", code, f"E-commerce {group}")
                cli("create-library", code, "shop", "Shop")
                reader = cli("register-app", code, "Reader")
                writer = cli("register-app", code, "Writer")
                cli("grant", code, reader["id"], "shop", "read")
                cli("grant", code, writer["id"], "shop", "write")
                readers[group] = {
                    "tenant": code, "label": "青禾商城" if group == "alpha" else "岚桥市集",
                    "key": reader["key"], "writer_key": writer["key"], "knowledge_base_code": "shop",
                }
                store_json(folder / "readers.json", readers)
                files = {}
                manifest["tenants"][code] = files
                for path in sorted((samples / group).iterdir()):
                    authorization = {"Authorization": f"Bearer {writer['key']}"}
                    response = client.post("/v1/knowledge-bases/shop/documents", params={"filename": path.name},
                                           headers={**authorization, "Idempotency-Key": path.name,
                                                    "Content-Type": "application/octet-stream"}, content=path.read_bytes())
                    job = response.raise_for_status().json()
                    files[path.name] = job
                    for _ in range(240):
                        state = client.get(f"/v1/ingestion-jobs/{job['job_id']}",
                                           headers=authorization).raise_for_status().json()
                        if state["keyword_status"] == "synced":
                            break
                        if state["state"] == "failed" or state["keyword_status"] == "failed":
                            raise RuntimeError(f"Ingestion failed for {path.name}: {state}")
                        time.sleep(0.25)
                    else:
                        raise RuntimeError(f"Indexing timed out for {path.name}")
                    print(f"{code}/shop/{path.name} indexed", flush=True)


def status() -> None:
    env = runtime()
    command(*COMPOSE, "ps", env=env)
    psql(env, "rag", "-c", """
        SELECT t.code AS tenant, l.code AS library, v.filename, d.revision, d.synced
        FROM rag.documents AS d
        JOIN rag.tenants AS t ON t.id = d.tenant
        JOIN rag.libraries AS l ON l.id = d.library
        JOIN rag.versions AS v ON v.id = d.published
        WHERE d.deleted IS NULL ORDER BY t.code, l.code, v.filename
    """)
    for path in sorted(RUNS.glob("*/run.json")):
        run = json.loads(path.read_text())
        print(f"{path.parent.name} {run['status']}: db={run['database']} es={run['index']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run and inspect the persistent rag-dev environment.")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("up", "status", "demo", "test", "serve"):
        actions.add_parser(action)
    measured = actions.add_parser("benchmark")
    measured.add_argument("--repeat", type=int, default=3)
    measured.add_argument("--bad-cases", action="store_true", help="Run the labelled retrieval failure cases")
    measured.add_argument("--paired", action="store_true", help="Compare candidate budgets on one index")
    measured.add_argument("--evidence", action="store_true", help="Check policy date and clarification cases")
    answers = actions.add_parser("answer-eval", help="Capture synthetic QA answers for offline evaluation")
    answers.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    answers.add_argument("--paired", action="store_true")
    answers.add_argument("--repeat", type=int, default=1)
    actions.add_parser("policy-eval", help="Check a versioned policy across Markdown, DOCX and PDF in isolation")
    actions.add_parser("sync-drill", help="Exercise sync delivery and recovery on isolated services")
    cleanup = actions.add_parser("retain", help="Preview or apply a tenant's version retention cleanup")
    cleanup.add_argument("tenant", type=short_code)
    cleanup.add_argument("--days", type=int, default=30)
    cleanup.add_argument("--limit", type=int, default=100)
    cleanup.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "benchmark":
        if args.repeat < 1:
            parser.error("--repeat must be positive")
        benchmark(args.repeat, bad_cases=args.bad_cases, paired=args.paired, evidence=args.evidence)
    elif args.action == "sync-drill":
        from .sync_drill import run

        run()
    elif args.action == "answer-eval":
        if args.repeat < 1:
            parser.error("--repeat must be positive")
        answer_eval(args.split, args.paired, args.repeat)
    elif args.action == "policy-eval":
        from .policy_eval import run

        run()
    elif args.action == "retain":
        if not 1 <= args.days <= 36500 or not 1 <= args.limit <= 1000:
            cleanup.error("use 1 to 36500 days and 1 to 1000 versions per batch")
        try:
            with psycopg.connect(runtime()["RAG_TEST_OWNER_URL"], connect_timeout=5, options=SCHEMA_OPTIONS) as conn:
                print(json.dumps(retain(conn, args.tenant, days=args.days, limit=args.limit, apply=args.apply)))
        except ConfigurationError as error:
            cleanup.exit(1, f"{error}\n")
        except psycopg.Error:
            cleanup.exit(1, "Retention database operation failed. Run rag.dev up to apply schema changes first.\n")
    elif args.action == "serve":
        RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = RUNS / "serve.log"
        try:
            with services(runtime(), 8001, log) as (url, processes):
                print(f"API documentation: {url}/docs", flush=True)
                print(f"Service logs: {log}", flush=True)
                while all(process.poll() is None for process in processes):
                    time.sleep(0.5)
                raise SystemExit("A RAG service exited.")
        except KeyboardInterrupt:
            pass
    else:
        {"up": up, "status": status, "demo": demo, "test": test}[args.action]()


if __name__ == "__main__":
    main()
