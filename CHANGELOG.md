# Changelog

This log records notable changes to the RAG project. Entries are grouped by impact under [Unreleased](#unreleased) until a tagged release.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases use [Semantic Versioning](https://semver.org/) and dated headings that match their Git tags.

## [Unreleased]

### Added

- Contributor guidance for commits on `main`, local verification, and changelog updates.
- FastAPI retrieval PoC with Markdown and text ingestion, pgvector and Elasticsearch, RRF, optional BGE reranking, and citations.
- A 30-question benchmark and executable checks for isolation, document ACLs, version changes, and stale index entries.
- RAGFlow 1.0.0 baseline with the same source files, a local BGE embedding endpoint, and comparable evidence and authorization probes.
- Phase-one design, an eight-table schema proposal, and a consolidated PoC validation report.
- RAG service with an independent database, tenant and application provisioning, API Key authentication, and knowledge base read and write grants.
- Alibaba Cloud Bailian client for document and query embeddings and text reranking, with a local model check.
- Markdown and plain-text upload, PostgreSQL ingestion jobs, and a Bailian embedding worker with retries and version publication.
- E-commerce sample documents and integration checks for ingestion, publication, and tenant isolation.
- Elasticsearch sync worker with revision-based cleanup, bounded retries, and searchable status in the ingestion job API.
- Business retrieval API with pgvector, Elasticsearch, RRF, Bailian reranking, current-version citations, and original downloads.
- Document reader controls, logical deletion, and a benchmark runner for the original 30 questions.
- Persistent `rag-dev` workflow for browsing sample data and retaining integration and benchmark runs.

### Changed

- Consolidate prototype Python modules and fixtures under `poc/` and use `poc.*` entry points for the API and verification commands.
- Organize phase-one design under `docs/plan/` and Compose and SQL files under `docs/env/`, with a dedicated run guide for the PoC.
- Store business document originals in PostgreSQL and run `rag-dev` with PostgreSQL and Elasticsearch.

### Security

- Tenant-scoped PostgreSQL policies and authorization checks before candidate text, citations, or retrieval results are returned.
