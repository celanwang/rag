# Changelog

This log records notable changes to the RAG project. Entries are grouped by impact under [Unreleased](#unreleased) until a tagged release.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases use [Semantic Versioning](https://semver.org/) and dated headings that match their Git tags.

## [Unreleased]

### Added

- Contributor guidance for commits on `main`, local verification, and changelog updates.
- FastAPI retrieval PoC with Markdown and text ingestion, pgvector and Elasticsearch, RRF, optional BGE reranking, and citations.
- A 30-question benchmark and executable checks for isolation, document ACLs, version changes, and stale index entries.
- RAGFlow 1.0.0 baseline with the same source files, a local BGE embedding endpoint, and comparable evidence and authorization probes.

### Security

- Tenant-scoped PostgreSQL policies and authorization checks before candidate text, citations, or retrieval results are returned.
