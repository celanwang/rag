# rag

A multi-tenant retrieval-augmented generation (RAG) service.

## Features

- Tenant and application provisioning with knowledge base read and write grants.
- Asynchronous Markdown and plain-text ingestion with version publication.
- Tenant-scoped document, version, and job storage.
- Asynchronous keyword index synchronization for published documents.

## Architecture

The service lives in `rag/`. FastAPI handles authentication and uploads. PostgreSQL tracks permissions, jobs, and published versions. The ingestion worker reads originals from S3-compatible storage and stores Bailian embeddings in pgvector. A separate worker synchronizes the currently published version to Elasticsearch.

The PoC provides reproducible hybrid retrieval benchmarks and a RAGFlow baseline. [Validation results](docs/poc-results.md)

[Phase-one design](docs/plan/design.md) · [Database schema](docs/plan/schema.md)

## Tech Stack

- Python and FastAPI
- PostgreSQL and pgvector
- Elasticsearch
- S3-compatible object storage
- Alibaba Cloud Bailian
- Docker Compose and uv

## Development

- [Run and verify the RAG service](rag/README.md)
- [Run and verify the PoC](poc/README.md)
- [Compose and database initialization files](docs/README.md#env)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
