# ADR 0004: Ship a single-process reference with replaceable state interfaces

- Status: accepted
- Date: 2026-09-03

## Context

Demonstrating distributed PostgreSQL, Redis, event streaming, and model providers would make local
execution expensive and brittle. Omitting state behavior entirely would make the gateway a routing
mock rather than an executable system.

## Decision

Use SQLite for artifacts/releases/evidence and in-process implementations for rate limits, semantic
cache, and circuits. Keep them behind focused classes and document that the Kubernetes manifest is
single replica until state is externalized.

## Consequences

- The full release workflow runs on a laptop without infrastructure or credentials.
- Tests are deterministic and fast.
- Multiple workers/replicas do not share quotas, cache, circuits, or durable state.
- Production scaling requires explicit state migrations rather than changing a replica count.

## Alternatives considered

**Require a full distributed stack:** rejected for the reference because setup cost would obscure
the policy/release mechanics.

**Use only mocks and no persistence:** rejected because immutability, rollback, evidence, and
aggregation need executable semantics.

## References

- [Architecture](../architecture.md)
- [Deployment guide](../deployment.md)
- `src/aegis_gateway/runtime.py`
