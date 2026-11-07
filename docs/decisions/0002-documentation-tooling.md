# ADR 0002: Documentation tooling

- **Status:** Accepted for the foundation milestone
- **Date:** 2026-11-07

## Context

Catalyst needs a browsable documentation site, API references, architecture diagrams, parity tables, and runnable examples. The project is Python-facing, so contributors should be able to build the site using the Python development environment.

## Decision

Use MkDocs with the Material theme. Keep documentation sources under `docs/`; render the initial site locally and keep the build strict so warnings fail the check.

## Rationale

MkDocs provides a low-friction Markdown workflow and integrates naturally with the Python tooling already required by Catalyst. Mermaid support can be added through the Material extension/plugin mechanism without imposing a Rust documentation-site toolchain.

## Consequences

- Pin MkDocs and theme/plugin requirements in the development dependency configuration.
- Add `mkdocs build --strict` to local CI when the skeleton is ready.
- Generate API references from Rust/Python sources as those public APIs are introduced.