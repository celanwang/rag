# RAG Service

The RAG service manages knowledge base access, asynchronously ingests Markdown and plain text, and synchronizes published versions to Elasticsearch. Run the commands from the repository root with Docker Compose, uv, and Python 3.11. [Compose config](../docs/env/rag/compose.yaml)

## Start local services

Generate local credentials once. A fresh volume initializes the eight business tables and the worker roles. PostgreSQL, S3, and Elasticsearch listen on `127.0.0.1` at ports `55433`, `59001`, and `59201`. Set `RAG_POSTGRES_PORT`, `RAG_S3_PORT`, or `RAG_ES_PORT` when running another local stack. [Access SQL](../docs/env/rag/schema.sql) · [Ingestion SQL](../docs/env/rag/ingestion.sql) · [Sync SQL](../docs/env/rag/sync.sql)

```sh
(
  umask 077
  touch docs/env/rag/.env
  for name in RAG_POSTGRES_PASSWORD RAG_API_PASSWORD RAG_ADMIN_PASSWORD RAG_WORKER_PASSWORD RAG_SYNC_PASSWORD RAG_S3_ACCESS_KEY RAG_S3_SECRET_KEY; do
    if ! grep -q "^$name=" docs/env/rag/.env; then
      printf '\n%s=%s\n' "$name" "$(openssl rand -hex 24)" >> docs/env/rag/.env
    fi
  done
)
docker compose --env-file docs/env/rag/.env -f docs/env/rag/compose.yaml up -d --wait
uv sync --locked --python 3.11
```

For an existing business database with only the access tables, apply the ingestion SQL once to keep the tenants and grants already provisioned.

```sh
docker compose --env-file docs/env/rag/.env -f docs/env/rag/compose.yaml exec -T postgres \
  psql -U postgres -d rag -f /docker-entrypoint-initdb.d/02-ingestion.sql
```

For an existing business database provisioned before Elasticsearch synchronization, apply the sync SQL once after the ingestion SQL. Existing queued sync jobs remain available to the new worker.

```sh
docker compose --env-file docs/env/rag/.env -f docs/env/rag/compose.yaml exec -T postgres \
  psql -U postgres -d rag -f /docker-entrypoint-initdb.d/03-sync.sql
```

In each terminal that uses storage, export the endpoint and local credentials. Create the private `rag-originals` bucket once.

```sh
. docs/env/rag/.env
export RAG_S3_ENDPOINT="http://127.0.0.1:${RAG_S3_PORT:-59001}" RAG_S3_ACCESS_KEY RAG_S3_SECRET_KEY
uv run --locked python -m rag.objects
```

## Configure access

The CLI uses the provisioning role. Application keys are issued once at registration. Tenant and library codes use lowercase letters, digits, and hyphens, starting with a letter or digit. `register-app --expires` accepts an ISO 8601 timestamp with a time zone.

```sh
. docs/env/rag/.env
export RAG_ADMIN_DATABASE_URL="postgresql://rag_admin:$RAG_ADMIN_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
uv run --locked python -m rag.cli create-tenant alpha Alpha
uv run --locked python -m rag.cli create-library alpha demo 'Alpha demo'
rag_application="$(uv run --locked python -m rag.cli register-app alpha Reader)"
rag_app_id="$(printf '%s' "$rag_application" | uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
rag_app_key="$(printf '%s' "$rag_application" | uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["key"])')"
unset rag_application
uv run --locked python -m rag.cli grant alpha "$rag_app_id" demo read
```

## Read a knowledge base

Start the API in a second terminal with the restricted role.

```sh
. docs/env/rag/.env
RAG_DATABASE_URL="postgresql://rag_api:$RAG_API_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag" \
  uv run --locked uvicorn rag.api:app --host 127.0.0.1 --port 8001
```

In the first terminal, the request returns `200` with the library `id`, `code`, and `name`. Repeat it after revoking `read` to see `403`.

```sh
curl -sS -i -H "Authorization: Bearer $rag_app_key" http://127.0.0.1:8001/v1/knowledge-bases/demo
uv run --locked python -m rag.cli revoke alpha "$rag_app_id" demo read
curl -sS -i -H "Authorization: Bearer $rag_app_key" http://127.0.0.1:8001/v1/knowledge-bases/demo
```

`read` and `write` are independent grants. Missing, invalid, expired, or disabled keys return `401`. A code absent from the caller's tenant returns `404`.

## Upload and publish

With the API running and the storage variables exported in its terminal, grant the application `write`. Send the original bytes with a `filename` query parameter and an `Idempotency-Key` header. The upload accepts UTF-8 `.md` and `.txt` up to 256 KiB.

```sh
uv run --locked python -m rag.cli grant alpha "$rag_app_id" demo write
rag_submission="$(curl -fsS -X POST \
  -H "Authorization: Bearer $rag_app_key" \
  -H 'Content-Type: text/markdown' -H 'Idempotency-Key: alpha-returns-v1' \
  --data-binary @docs/samples/ecommerce/alpha/returns.md \
  'http://127.0.0.1:8001/v1/knowledge-bases/demo/documents?filename=returns.md')"
rag_job_id="$(printf '%s' "$rag_submission" | uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["job_id"])')"
```

The API returns `202` with `job_id`, `document_id`, and `version_id`. Repeating the same upload with the same key returns those IDs. A different upload with the same key returns `409`. To update an existing document, pass its `document_id` as another query parameter and use a new key.

Run the worker in a terminal with the storage variables exported and the Bailian key in the repository root `.env`.

```sh
. docs/env/rag/.env
export RAG_WORKER_DATABASE_URL="postgresql://rag_worker:$RAG_WORKER_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
uv run --locked --env-file .env python -m rag.worker
```

Check the job from the upload terminal. After publication it has `state=done` and `stage=published`. A temporary model or storage error schedules a retry, up to four total attempts. An expired lease makes the job claimable again.

```sh
curl -fsS -H "Authorization: Bearer $rag_app_key" "http://127.0.0.1:8001/v1/ingestion-jobs/$rag_job_id"
```

With the admin database and storage variables exported, `uv run --locked python -m rag.objects --prune` removes unreferenced originals older than 24 hours.

## Synchronize the keyword index

Start the independent sync worker. It uses PostgreSQL and Elasticsearch. Set `RAG_ES_INDEX` to override the default index `rag-chunks-v1` and `RAG_ES_API_KEY` when the cluster requires API key authentication.

```sh
. docs/env/rag/.env
export RAG_SYNC_DATABASE_URL="postgresql://rag_sync:$RAG_SYNC_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_ES_URL="http://127.0.0.1:${RAG_ES_PORT:-59201}"
uv run --locked python -m rag.sync
```

Add `--once` to process one available task. Query the ingestion job again to see `keyword_status=synced` after the worker writes the current version, removes older chunks, and refreshes the index. `keyword_attempts` and `keyword_error` describe the sync task for the current revision. Failed Elasticsearch operations retry independently of ingestion, up to four attempts.

## Verify authorization

Run the checks against a disposable local database. They provision isolated test tenants through the CLI and exercise the API and PostgreSQL policies.

```sh
. docs/env/rag/.env
export RAG_ADMIN_DATABASE_URL="postgresql://rag_admin:$RAG_ADMIN_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_DATABASE_URL="postgresql://rag_api:$RAG_API_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
uv run --locked python -m unittest discover -s tests -p test_authorization.py -v
```

For the full suite, use a disposable database, bucket, and Elasticsearch instance with both workers stopped. The tests use the e-commerce samples, exercise real storage and search, and inject model and Elasticsearch failures at the HTTP boundary.

```sh
export RAG_WORKER_DATABASE_URL="postgresql://rag_worker:$RAG_WORKER_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_SYNC_DATABASE_URL="postgresql://rag_sync:$RAG_SYNC_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_TEST_OWNER_URL="postgresql://postgres:$RAG_POSTGRES_PASSWORD@127.0.0.1:${RAG_POSTGRES_PORT:-55433}/rag"
export RAG_ES_URL="http://127.0.0.1:${RAG_ES_PORT:-59201}"
uv run --locked python -m unittest discover -s tests -v
```

## Verify Bailian model access

The model client uses `text-embedding-v4` for 512-dimensional document and query vectors and `qwen3-rerank` for reranking. Put `DASHSCOPE_API_KEY` in the local, Git-ignored `.env` file at the repository root. Set `DASHSCOPE_HTTP_BASE_URL` if your key uses a different DashScope region or a workspace endpoint. [Bailian endpoints](https://help.aliyun.com/zh/model-studio/base-url)

Run this check with synthetic text to verify both models.

```sh
uv run --locked --env-file .env python -m rag.models
```
