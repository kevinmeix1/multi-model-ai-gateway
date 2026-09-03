# Contributing

Changes are welcome when they preserve the gateway's policy and provenance invariants. A feature is
not complete because one provider accepts the request; it needs a provider-independent contract,
failure behavior, evidence, tests, and documentation.

## Development setup

```bash
git clone https://github.com/kevinmeix1/multi-model-ai-gateway.git
cd multi-model-ai-gateway
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
python scripts/seed_demo.py
make check
```

No provider key is needed for tests or the default service. Hosted routes are disabled to prevent an
ordinary test run from sending data or spending credits.

## Design rules

Changes must preserve these rules:

1. Route policy is evaluated before preference, canary, or fallback.
2. Provider-specific request/response types stay inside adapters.
3. Cache reuse cannot cross tenant, policy, release, or forced-route scope.
4. A stream never changes model after exposing content.
5. Artifact versions are immutable.
6. Candidate decisions use durable evidence with explicit denominators.
7. Logs and metrics do not contain credentials, prompts, or model output by default.

If a proposal intentionally changes one, write an architecture decision record and update the threat
model before code review.

## Before opening a pull request

```bash
make format
make check
```

The pull request should explain:

- the problem and affected request/release path;
- policy, privacy, cost, and latency consequences;
- new failure modes and rollback behavior;
- tests added, including negative/failure cases;
- documentation and ADR changes;
- whether provider credentials or network access are required;
- any migration or compatibility impact.

Do not include real prompts, customer data, provider responses, account IDs, or credentials in test
fixtures.

## Adding a provider adapter

1. Implement `ProviderAdapter` in `src/aegis_gateway/providers/`.
2. Keep provider authentication, payloads, event types, and raw errors inside that module.
3. Normalize complete output, tokens, model identity, finish reason, and typed errors.
4. Parse streams incrementally; do not assume transport chunks match SSE/NDJSON records.
5. Respect the request deadline and close owned clients.
6. Register the adapter in `create_runtime`.
7. Add disabled catalogue examples with non-zero prices only after verification.
8. Add in-memory transport tests for complete, stream, auth, rate limit, timeout, 4xx/5xx, malformed
   events, and terminal stream errors.
9. Update the provider guide and compatibility matrix.

Do not add implicit SDK retries. The gateway service owns fallback and needs to account for every
attempt.

## Adding a request capability

A capability such as tool use or vision crosses several boundaries:

- typed request and response domain models;
- capability inference and route filtering;
- every provider adapter or an explicit unsupported result;
- streaming representation;
- cache identity;
- schema/evidence model;
- security authorization and prompt-injection analysis;
- offline datasets and API documentation.

Avoid accepting arbitrary provider JSON in the native API. Define the smallest provider-independent
contract with clear semantics.

## Changing routing policy

Routing changes need tests for both selection and rejection. Include:

- one worked numerical example;
- behavior at zero cost and minimum latency budgets;
- preferred and forced route behavior;
- circuit and fallback interaction;
- routing-regret interpretation;
- backtest or evaluation evidence for changed weights/priors.

Hard privacy, residency, and authorization constraints must remain outside learned or weighted
scoring.

## Changing cache identity

Treat removal of a scope field as a security-sensitive change. Explain why two requests that differ
in that field are guaranteed to have equivalent output and policy. Add cross-scope negative tests.

When adding generation fields, tool permissions, retrieval corpora, guardrail versions, or release
namespaces, update cache identity before enabling caching for them.

## Evaluation datasets

- Register a new version instead of mutating an existing one.
- Use stable, descriptive case IDs.
- Tag task, language, risk, and failure domain.
- Keep sensitive production content out unless an approved encrypted evaluation system exists.
- Separate prompt-tuning data from final holdout data.
- Document scorer limitations and inspect per-case deltas.

## Database changes

The reference initializes schema in `Database.initialize`. A schema change must be idempotent for a
new database and tested against existing data. For non-trivial evolution, introduce a migration tool
rather than accumulating conditional DDL.

Preserve artifact uniqueness, atomic release transitions, evidence append semantics, and rollback
auditability.

## Documentation style

- Describe the implementation that exists, then label future work explicitly.
- Prefer exact field names, formulas, failure modes, and examples over adjectives.
- State trade-offs and known limitations.
- Keep Mermaid diagrams small enough to read in GitHub's normal width.
- Link related guides instead of repeating whole sections.
- Run `python scripts/check_docs.py` before commit.

## Commit hygiene

Keep generated databases, `.env`, caches, coverage output, and credentials out of commits. Use a
focused commit message such as:

```text
feat(router): add deadline-aware candidate admission
fix(cache): include release namespace in identity
docs: explain shadow traffic evidence
test(provider): cover split SSE frames
```

## Reporting security issues

Do not open a public issue containing a credential, exploit payload, customer prompt, or undisclosed
vulnerability. Follow [SECURITY.md](SECURITY.md).
