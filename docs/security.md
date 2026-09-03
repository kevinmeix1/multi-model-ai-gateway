# Security and privacy threat model

## Trust boundaries

- Client input is untrusted.
- Model output and provider stream events are untrusted.
- Route catalogs and immutable registries are trusted only after code review and deployment signing.
- Provider credentials belong in a secret manager or workload identity system, never source control.
- Control-plane operations require a separate administrative credential.

## Defenses implemented

- Pydantic v2 rejects unknown request fields and constrains lengths and budgets.
- Confidential and restricted data automatically strengthen provider privacy requirements.
- Route selection cannot relax capability, residency, context, latency, or cost constraints.
- Tenant identity is part of cache scope; cross-tenant semantic hits are impossible by construction.
- Provider error bodies are truncated and normalized before reaching clients.
- Logs exclude API-key-named attributes and never serialize settings.
- Immutable artifact versions prevent silent prompt or dataset replacement.
- Control-plane bearer comparison is constant time.
- Unknown provider stream events are ignored safely; terminal errors are normalized.

## Required hardening before public deployment

1. Put the data plane behind OIDC/mTLS and derive `tenant_id` from authenticated claims rather than
   accepting it from a request body.
2. Replace the single admin token with scoped RBAC, short-lived credentials, and audited mutations.
3. Store secrets in a cloud secret manager or use workload identity; rotate them automatically.
4. Restrict provider egress using network policy and DNS controls.
5. Encrypt registry and evidence stores with managed keys and enforce retention/deletion policies.
6. Replace in-process cache and limiter state with tenant-aware distributed services.
7. Sign deployment artifacts and produce an SBOM; scan dependencies and container layers.
8. Redact or hash user identifiers and prompt data before observability export.
9. Add prompt-injection and data-exfiltration evaluation cases for tool-enabled routes.
10. Require human approval for changes to privacy mappings or fallback eligibility.

## Abuse cases to test

| Threat | Test |
|---|---|
| Cross-tenant cache probing | Similar prompts from two tenants never share a result |
| Region downgrade | EU-only request rejects global-only route |
| Restricted-data exfiltration | Restricted request can select only local-only route |
| Budget bypass | Candidate whose worst-case token cost exceeds budget is rejected |
| Retry amplification | Open circuit excludes failing route before provider invocation |
| Mid-stream model switch | Failure after first delta ends stream with typed interruption |
| Registry tampering | Same artifact version with different content returns conflict |

## Secret handling

`.env` is ignored. `.env.example` contains names only. CI runs a credential-pattern scan over tracked
text. If a credential is ever committed, remove it from history only after revoking and rotating it;
history rewriting does not invalidate a live secret.
