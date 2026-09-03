# ADR 0005: Normalize provider protocols behind adapters

- Status: accepted
- Date: 2026-09-03

## Context

Providers use different request fields, message-role semantics, response envelopes, usage records,
streaming protocols, event names, structured-output controls, and error taxonomies. Allowing these
types into routing or release code couples policy to every upstream API.

## Decision

Define provider-independent complete and streaming contracts. Each adapter owns authentication,
payload construction, response/event parsing, transport deadlines, health, and error normalization.
Fallback remains outside adapters where the eligible candidate set is visible.

## Consequences

- Routing and evaluation use one result/error vocabulary.
- Provider contract tests can run with in-memory HTTP transports.
- Provider-only features are not automatically exposed; they need an explicit normalized contract.
- Adapter maintenance is required when an upstream API changes.

## Alternatives considered

**Common third-party SDK abstraction:** rejected as the architectural boundary because it can hide
provider error/event semantics and introduces another release dependency. Individual adapters may
use SDKs later if they preserve the internal contract.

**Provider SDK objects throughout the service:** rejected because every policy and telemetry path
would branch on provider type.

## References

- [Provider adapter contract](../provider-adapters.md)
- `src/aegis_gateway/providers/`
