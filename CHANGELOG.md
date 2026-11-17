# Changelog

This log records notable changes to the RAG project. Entries are grouped by impact under [Unreleased](#unreleased) until a tagged release.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases use [Semantic Versioning](https://semver.org/) and dated headings that match their Git tags.

## [Unreleased]

### Added

- Fixed-candidate RRF replay, paired candidate and reranker benchmarks, and source-span evidence coverage.
- Operator keyword index repair, optional Celery executors for sync and ingest, and a recoverable index generation workflow.
- Scoped policy date selection, conflict evidence, clarification hints, and authorized same-section supplemental evidence.

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
- Tenant-scoped retention cleanup for expired PostgreSQL document versions, originals, and chunks with a dry run and preserved upload receipts.
- Eight labelled retrieval failure cases and an isolated benchmark for candidate coverage and misleading top results.
- Eleven follow-up retrieval cases covering question variants, policy date boundaries, and source precedence.
- A local web workbench for workspace switching, retrieval diagnostics, document browsing, and ingestion job tracking.
- Authorized knowledge base and published-document catalog endpoints with filename filtering and pagination.

### Changed

- Consolidate prototype Python modules and fixtures under `poc/` and use `poc.*` entry points for the API and verification commands.
- Organize phase-one design under `docs/plan/` and Compose and SQL files under `docs/env/`, with a dedicated run guide for the PoC.
- Store business document originals in PostgreSQL and run `rag-dev` with PostgreSQL and Elasticsearch.
- Consolidate the RAG database bootstrap into one SQL baseline, upgrade retained databases by schema version, and group implementation and verification reports under `docs/other/`.
- Give higher-ranked recall candidates more influence in RRF to preserve early evidence found by a single branch.
- Validate both benchmark suites against one JSON schema and report which gold chunks were missed during recall or fell outside the prefinal cutoff.
- Align the Python project name with `rag`.

### Removed

- Retire the one-time PostgreSQL baseline and original-file migration scripts after upgrading `rag-dev`.

### Security

- Tenant-scoped PostgreSQL policies and authorization checks before candidate text, citations, or retrieval results are returned.
- JSON audit events for authorization changes and document deletion, with request IDs propagated through ingestion and Elasticsearch synchronization.
- Keep application credentials in the local proxy and enforce workspace-scoped read and write access in the API.
