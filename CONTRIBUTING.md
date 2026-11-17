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

Run `uv lock --check` after dependency changes. For retrieval or storage changes, run the self-built PoC isolation checks and the relevant benchmark. For RAGFlow changes, run the baseline comparison. Record the verification commands and results with the change or its review. [Local verification](poc/README.md)

Reference the design for changes that affect interfaces, authorization, or document lifecycle. [System design](docs/plan/design.md)

## Changelog

Record notable product or design changes in English under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md). Use the applicable categories from Keep a Changelog, including Added, Changed, Deprecated, Removed, Fixed, and Security. Group entries by user impact and include maintenance changes when they affect contributors. At release time, move the entries to a dated version section that matches the Git tag. [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
