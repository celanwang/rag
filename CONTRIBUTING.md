# Contributing

## Main branch

Commit project changes directly to `main`. If a pull request is used for review, target `main`.

## Commits

Use one English line in Conventional Commits format. Keep each commit focused on one concern. [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)

```text
feat(poc): add tenant-scoped hybrid retrieval
test(poc): verify cross-tenant citation access
docs(design): define asynchronous document ingestion
```

## Verification

Run `uv lock --check` after dependency changes. Run business integration checks and benchmarks through `rag.dev` so each run is retained in the local `rag-dev` environment. Run the relevant PoC checks when changing a baseline. Record the verification commands and results with the change or its review. [Business verification](rag/README.md#persistent-rag-dev) · [PoC verification](poc/README.md)

Reference the design for changes that affect interfaces, authorization, or document lifecycle. [System design](docs/plan/design.md)

## Changelog

Record notable product or design changes in English under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md). Use the applicable categories from Keep a Changelog, including Added, Changed, Deprecated, Removed, Fixed, and Security. Group entries by user impact and include maintenance changes when they affect contributors. At release time, move the entries to a dated version section that matches the Git tag. [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
