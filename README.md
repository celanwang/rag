# rag

A multi-tenant retrieval-augmented generation (RAG) project. The current prototype retrieves authorized document excerpts with source citations.

## Features

- Markdown and plain-text ingestion.
- Keyword, vector, and hybrid search with optional reranking.
- Tenant isolation and document-level access control.
- Reproducible retrieval benchmarks and a RAGFlow baseline.

## Architecture

FastAPI serves the ingestion and retrieval API. PostgreSQL stores document versions, vectors, and access rules. Elasticsearch serves keyword search. Hybrid retrieval combines both result sets and verifies access before returning evidence.

[Phase-one design](docs/plan/design.md) · [Database schema](docs/plan/schema.md)

## Tech Stack

- Python and FastAPI
- PostgreSQL, pgvector, and Elasticsearch
- ONNX Runtime
- Docker Compose and uv

## Development

- [Run and verify the PoC](poc/README.md)
- [Validation results](docs/poc-results.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
