# rag

A multi-tenant retrieval-augmented generation (RAG) service.

## Features

- Tenant and application provisioning with knowledge base read and write grants.
- Asynchronous Markdown and plain-text ingestion with version publication.
- Tenant-scoped document, version, and job storage.
- Asynchronous keyword index synchronization for published documents.
- Vector, keyword, and hybrid retrieval with optional reranking, source citations, and original downloads.
- Cited question answering with an optional second lookup and offline RAGAS evaluation.
- Local web workbench for tenant-scoped retrieval, document browsing, and ingestion.

## Architecture

The service lives in `rag/`. PostgreSQL stores permissions, document versions, original bytes, and jobs. The ingestion worker stores Bailian embeddings in pgvector. A separate worker synchronizes the current version to Elasticsearch. The FastAPI retrieval endpoint combines both recall paths and checks evidence against PostgreSQL.

The React workbench runs on loopback. Its local proxy attaches the selected workspace's application credentials to FastAPI requests. [Local workbench](frontend/README.md)

The PoC provides reproducible hybrid retrieval benchmarks and a RAGFlow baseline. [Validation results](docs/other/poc-results.md)

[Phase-one design](docs/plan/design.md) · [Database schema](docs/plan/schema.md)

## Tech Stack

- Python and FastAPI
- React, TypeScript, and Vite
- PostgreSQL and pgvector
- Elasticsearch
- Alibaba Cloud Bailian
- Docker Compose and uv

## Development

- [Start and inspect rag-dev](rag/README.md#persistent-rag-dev)
- [Run the local web workbench](frontend/README.md)
- [Business retrieval benchmark](docs/other/retrieval-results.md)
- [Cited question answering](rag/README.md#answer-with-citations)
- [Run and verify the PoC](poc/README.md)
- [Compose and database initialization files](docs/README.md#env)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
