# ADR 0002: Never switch providers after exposing content

- Status: accepted
- Date: 2026-09-03

## Context

Fallback before output can hide a provider outage. After output begins, another model does not share
the failed model's hidden state, tokenization, or continuation distribution. Concatenating both
outputs produces ambiguous provenance and may invalidate a structured result.

## Decision

The first content delta is a commit point. Before it, Aegis may try another eligible candidate. After
it, provider failure raises `stream_interrupted`; final schema failure also terminates without
fallback.

## Consequences

- Every exposed stream has one route/model provenance.
- Clients must handle partial output and decide whether to retry from scratch.
- Availability is lower than a splice-and-continue implementation, but semantics are honest.
- Structured streaming remains optimistic until final validation.

## Alternatives considered

**Continue with another model:** rejected because the combined text has no coherent generation or
evaluation identity.

**Buffer all output before returning:** valid for strict consumers, but removes streaming TTFT. The
non-streaming endpoint provides this behavior.

## References

- [Request lifecycle](../request-lifecycle.md)
- `src/aegis_gateway/control/service.py`
