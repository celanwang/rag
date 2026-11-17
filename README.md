# rag

A multi-tenant retrieval-augmented generation (RAG) project with a knowledge base access service and a retrieval PoC.

## Features

- Tenant and application provisioning with knowledge base read and write grants.
- Markdown and plain-text ingestion.
- Keyword, vector, and hybrid search with optional reranking.
- Tenant isolation and document-level access control.
- Reproducible retrieval benchmarks and a RAGFlow baseline.

## Architecture

The RAG service uses FastAPI and PostgreSQL for application authentication and tenant-scoped knowledge base access. The retrieval PoC uses pgvector and Elasticsearch for hybrid search and verifies access before returning evidence.

[Phase-one design](docs/plan/design.md) · [Database schema](docs/plan/schema.md)

## Tech Stack

- Python and FastAPI
- PostgreSQL, pgvector, and Elasticsearch
- ONNX Runtime
- Docker Compose and uv

## Development

- [Configure and verify the RAG service](rag/README.md)
- [Run and verify the PoC](poc/README.md)
- [Compose and database initialization files](docs/README.md#env)
- [Validation results](docs/poc-results.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
