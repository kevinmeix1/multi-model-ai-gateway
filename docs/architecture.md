# Architecture and invariants

## System boundaries

Aegis has three logical planes:

1. **Data plane:** validates requests, resolves immutable prompts, rate-limits tenants, checks the
   semantic cache, selects a route, invokes one provider, validates output, and streams or returns it.
2. **Control plane:** manages artifacts, evaluations, releases, canary percentages, shadow traffic,
   circuit state, and rollback decisions. It never handles provider payloads directly.
3. **Evidence plane:** records request outcomes and evaluation runs. Release decisions read this
   ledger rather than mutable dashboard state.

Provider APIs are outside the trust boundary. Their status codes, stream events, token counts, model
identifiers, and outputs are untrusted data normalized by adapters.

## Request lifecycle

```text
HTTP validation
  -> immutable prompt resolution
  -> tenant token bucket
  -> policy-safe cache lookup
  -> release assignment (baseline/canary/shadow)
  -> hard route filters
  -> utility ranking
  -> circuit admission
  -> provider adapter
  -> JSON Schema validation
  -> evidence write + Prometheus observation
  -> cache insert
  -> asynchronous shadow and release assessment
```

Hard filters always execute before optimization. A cheap provider is not eligible if it violates a
privacy, region, context, capability, latency, or cost requirement.

## Routing model

For route `r`, estimated maximum request cost is

```text
C(r) = (T_in * price_in(r) + T_out,max * price_out(r)) / 1,000,000
```

Eligible routes are ranked by

```text
U(r) = 0.55 * quality(r)
     - 0.25 * min(1, expected_p95(r) / latency_budget)
     - 0.20 * min(1, C(r) / cost_budget)
```

The weights are explicit configuration, not learned truth. Routing regret is the utility gap between
the selected route and the highest-utility eligible route. Canary policy may intentionally incur
regret to gather evidence.

Token estimation is deliberately conservative and provider-independent. Actual cost uses
provider-reported token counts. Production deployments should replace the estimator with the exact
tokenizer for every model family while retaining the same route contract.

## Privacy mapping

| Classification | Minimum route privacy |
|---|---|
| Public / internal | Requested privacy mode |
| Confidential | Zero-retention unless local-only was explicitly requested |
| Restricted | Local-only |

An allowed region must intersect the route's declared regions. `global` is not interpreted as every
region because that would silently weaken residency guarantees.

## Fallback invariant

The fallback list is the already-filtered candidate list. Aegis never converts a region-restricted,
structured-output, or local-only request into an unconstrained retry.

Before the first streamed content delta, an upstream failure may move to another eligible route.
After a delta is exposed, model switching would create an output with ambiguous provenance and
potentially inconsistent semantics, so Aegis terminates the stream with a typed error.

## Cache identity

Lookup scope includes tenant, classification, privacy mode, response schema hash, capabilities,
maximum output tokens, and prompt version. Embeddings are compared only inside that scope. The local
implementation uses deterministic feature hashing and cosine similarity; the `EmbeddingBackend`
protocol permits a real embedding service without weakening isolation.

## State ownership

| State | Owner | Consistency requirement |
|---|---|---|
| Route catalog | Deployment configuration | Versioned and reviewed |
| Prompt/dataset/model/policy artifacts | Artifact registry | Immutable by composite key |
| Release pointer | Release registry | Transactional transition |
| Circuit state | Gateway replica | Fast local protection; aggregate externally at scale |
| Rate-limit state | Gateway replica | Replace with distributed atomic state at scale |
| Cache entries | Gateway replica | Best effort, never source of truth |
| Request/evaluation evidence | Evidence store | Durable before release assessment |

## Availability and consistency choices

- A registry outage blocks new releases but should not invalidate an already loaded route catalog.
- A metrics failure is treated as an operational fault because unobserved canary traffic is unsafe.
- Cache failure degrades to a miss.
- Shadow failure never changes the user response, but it is recorded separately.
- Artifact conflicts fail closed; an existing version cannot be overwritten.
