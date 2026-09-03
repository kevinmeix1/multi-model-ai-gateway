# ADR 0003: Store control artifacts as immutable versions

- Status: accepted
- Date: 2026-09-03

## Context

Evaluation and release evidence is meaningless when a prompt or dataset can change under the same
name/version after a run. Mutable "current" records make incidents difficult to reproduce and allow
silent gate changes.

## Decision

Identify artifacts by `(kind, name, version)`, canonicalize JSON content, and store a SHA-256 hash.
Re-registering identical content is idempotent; different content under the same key is a conflict.

## Consequences

- Releases and evaluations can refer to stable inputs.
- Every meaningful edit needs a new version.
- "Latest" remains a convenience lookup and should not be used for reproducible release manifests.
- The registry needs retention policy because old versions accumulate.

## Alternatives considered

**Mutable row plus update timestamp:** rejected because timestamps do not prevent or identify
content replacement reliably.

**Git-only artifacts:** useful for review, but runtime evaluation needs a validated, queryable
artifact contract. A production registry should also record source commit.

## References

- [Data model and persistence](../data-model.md)
- `src/aegis_gateway/control/registry.py`
