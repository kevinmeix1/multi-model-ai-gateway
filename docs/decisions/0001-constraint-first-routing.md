# ADR 0001: Apply hard constraints before utility ranking

- Status: accepted
- Date: 2026-09-03

## Context

Requests differ in required capabilities, data classification, privacy mode, processing region,
context size, latency budget, and cost ceiling. A weighted score that mixes these properties can
select a cheap or high-quality route by compensating for a policy violation.

## Decision

Build a feasible route set using hard Boolean predicates. Rank only that set with an inspectable
quality/latency/cost utility. Preferred routes, canaries, forced evaluation, and fallback cannot add a
route to the feasible set.

## Consequences

- The gateway fails closed when no route satisfies the contract.
- Rejection reasons remain available for diagnosis.
- Policy and optimization can evolve independently.
- Catalogue accuracy becomes security-critical.
- Some requests fail even when a technically reachable model could answer them.

## Alternatives considered

**One weighted score:** rejected because no finite privacy or residency penalty is truly hard.

**Provider-first fallback list:** rejected because availability order cannot express request-specific
constraints.

**Learned end-to-end router:** deferred. A learned model may rank feasible routes later, but it must
not learn legal/privacy eligibility.

## References

- [Routing policy](../routing-policy.md)
- `src/aegis_gateway/control/router.py`
