import argparse
import logging
import os
import time
from uuid import UUID

import psycopg
from celery import Celery
from kombu.exceptions import OperationalError
from psycopg import sql

from .db import SCHEMA_OPTIONS
from .jobs import LEASE_SECONDS, Kind, claim
from .keyword import KeywordIndex
from .models import BailianModels
from .sync import process
from .worker import process as ingest

logger = logging.getLogger(__name__)
BROKER = os.getenv("RAG_CELERY_BROKER", "redis://127.0.0.1:56379/0")
QUEUE = os.getenv("RAG_CELERY_QUEUE", "rag-sync")
INGEST_QUEUE = os.getenv("RAG_CELERY_INGEST_QUEUE", "rag-ingest")
QUEUES = {"sync": QUEUE, "ingest": INGEST_QUEUE}
TASK_TIMEOUT = int(os.getenv("RAG_SYNC_TASK_TIMEOUT", "100"))
SYNC_LEASE = int(os.getenv("RAG_SYNC_LEASE_SECONDS", str(LEASE_SECONDS)))
VISIBILITY_TIMEOUT = 900
if not 0 < TASK_TIMEOUT < SYNC_LEASE < VISIBILITY_TIMEOUT:
    raise ValueError(f"Sync needs 0 < task timeout < lease < Redis visibility timeout ({VISIBILITY_TIMEOUT} seconds)")
app = Celery("rag", broker=BROKER)
app.conf.update(
    task_default_queue=QUEUE,
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_time_limit=TASK_TIMEOUT,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": VISIBILITY_TIMEOUT, "socket_connect_timeout": 2, "socket_timeout": 2,
    },
)


def connect(kind: Kind = "sync"):
    return psycopg.connect(
        os.environ["RAG_SYNC_DATABASE_URL" if kind == "sync" else "RAG_WORKER_DATABASE_URL"],
        autocommit=True, options=SCHEMA_OPTIONS, connect_timeout=5,
    )


def reserve(conn: psycopg.Connection, kind: Kind = "sync") -> list[UUID]:
    query = sql.SQL("SELECT * FROM {}(32)").format(sql.Identifier("rag", f"dispatch_{kind}"))
    return [row[0] for row in conn.execute(query)]


def send(job_id: UUID, kind: Kind = "sync") -> None:
    app.send_task(f"rag.{kind}", args=(str(job_id),), queue=QUEUES[kind], retry=False)


@app.task(name="rag.sync")
def sync_job(job_id: str) -> None:
    with connect() as conn:
        lease = claim(conn, "sync", UUID(job_id), lease_seconds=SYNC_LEASE)
        if lease is not None:
            with KeywordIndex.from_env() as index:
                process(conn, lease, index)


@app.task(name="rag.ingest", time_limit=600)
def ingest_job(job_id: str) -> None:
    with connect("ingest") as conn:
        lease = claim(conn, "ingest", UUID(job_id))
        if lease is not None:
            with BailianModels.from_env() as models:
                ingest(conn, lease, models)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch due PostgreSQL jobs to Celery")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--kind", choices=QUEUES, default="sync")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            with connect(args.kind) as conn:
                while True:
                    jobs = reserve(conn, args.kind)
                    for job_id in jobs:
                        send(job_id, args.kind)
                    if args.once:
                        return
                    if not jobs:
                        time.sleep(0.1)
        except (OperationalError, OSError, psycopg.Error) as error:
            logger.warning("Task dispatch unavailable (%s)", type(error).__name__)
            if args.once:
                raise SystemExit(1) from None
            time.sleep(1)


if __name__ == "__main__":
    main()
