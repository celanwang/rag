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

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "docs/env/rag/.env"
COMPOSE = (
    "docker", "compose", "-p", "rag-dev", "--env-file", str(ENV_FILE),
    "-f", str(ENV_FILE.with_name("compose.yaml")),
)
RUNS = ROOT / ".rag/dev"
SQL = (
    ("SELECT to_regclass('rag.tenants') IS NOT NULL", "01-schema.sql"),
    ("SELECT to_regclass('rag.jobs') IS NOT NULL", "02-ingestion.sql"),
    ("SELECT to_regprocedure('rag.claim_sync(integer,integer)') IS NOT NULL", "03-sync.sql"),
    ("SELECT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = 'rag.chunks'::regclass AND polname = 'api_read_chunks')",
     "04-retrieval.sql"),
)
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
    postgres = container_env("rag-dev-postgres-1")
    recorded = postgres.copy()
    recorded["RAG_POSTGRES_PASSWORD"] = postgres.get("POSTGRES_PASSWORD")
    if not postgres and not all(values.get(name) for name in SECRETS[:3]):
        volume = subprocess.run(
            ["docker", "volume", "inspect", "rag-dev_postgres-data"], capture_output=True, check=False,
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
    env["RAG_ES_INDEX"] = local.get("RAG_ES_INDEX", "rag-chunks-v1")
    return env


def psql(env: dict, database: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return command(
        *COMPOSE, "exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", database, *args, env=env, **kwargs,
    )


def up() -> None:
    configure()
    env = runtime()
    command(*COMPOSE, "up", "-d", "--wait", "--wait-timeout", "90", env=env)
    for check, filename in SQL:
        present = psql(env, "rag", "-Atc", check, capture_output=True, text=True).stdout.strip()
        if present == "f":
            psql(env, "rag", "-q", "-f", f"/docker-entrypoint-initdb.d/{filename}")
    legacy = psql(env, "rag", "-Atc", """
        SELECT EXISTS (SELECT 1 FROM pg_attribute
                       WHERE attrelid = 'rag.versions'::regclass AND attname = 'path' AND NOT attisdropped)
    """, capture_output=True, text=True).stdout.strip()
    if legacy == "t":
        raise SystemExit("Run the original-file migration in the runbook before starting the RAG service.")
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
    manifest = {
        "kind": kind, "run": stamp, "status": "running", "database": database,
        "index": env["RAG_ES_INDEX"],
    }
    store_json(folder / "run.json", manifest)
    print(f"{kind}: database={database}, index={manifest['index']}", flush=True)
    print(folder, flush=True)
    try:
        if kind != "demo":
            snapshot = command(
                *COMPOSE, "exec", "-T", "postgres", "pg_dump", "-U", "postgres", "-d", "rag",
                "--schema-only", "--schema=rag", capture_output=True, env=env,
            ).stdout
            command(*COMPOSE, "exec", "-T", "postgres", "createdb", "-U", "postgres", database, env=env)
            psql(env, database, "-qc", "CREATE EXTENSION vector WITH SCHEMA public")
            psql(env, database, "-q", "--single-transaction", input=snapshot)
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


@contextmanager
def services(env: dict, port: int, log: Path | None = None):
    if not env.get("DASHSCOPE_API_KEY"):
        raise SystemExit("Set DASHSCOPE_API_KEY or pass --env-file .env to uv.")
    stream = log.open("wb") if log else None
    processes = []
    try:
        for args in (
            ("uvicorn", "rag.api:app", "--host", "127.0.0.1", "--port", str(port)),
            ("rag.worker",), ("rag.sync",),
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


def benchmark(repeat: int) -> None:
    with new_run("benchmark") as (env, folder, _), services(env, available_port(), folder / "services.log") as (url, _):
        command(
            sys.executable, "-m", "rag.benchmark", "--url", url, "--repeat", str(repeat),
            "--out", str(folder / "benchmark.json"), env=env,
        )


def demo() -> None:
    with new_run("demo") as (env, folder, manifest), services(env, available_port(), folder / "services.log") as (url, _):
        def cli(*args: str) -> dict:
            return json.loads(command(sys.executable, "-m", "rag.cli", *args, env=env, capture_output=True).stdout)

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
                readers[group] = {"tenant": code, "key": reader["key"], "knowledge_base_code": "shop"}
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
    args = parser.parse_args()
    if args.action == "benchmark":
        if args.repeat < 1:
            parser.error("--repeat must be positive")
        benchmark(args.repeat)
    elif args.action == "serve":
        try:
            with services(runtime(), 8001) as (url, processes):
                print(f"API documentation: {url}/docs", flush=True)
                while all(process.poll() is None for process in processes):
                    time.sleep(0.5)
                raise SystemExit("A RAG service exited.")
        except KeyboardInterrupt:
            pass
    else:
        {"up": up, "status": status, "demo": demo, "test": test}[args.action]()


if __name__ == "__main__":
    main()
