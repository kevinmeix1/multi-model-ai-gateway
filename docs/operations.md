# Operations and incident runbook

## Initial service objectives

These are starting targets for the deterministic/local deployment, not measured production claims:

- Gateway availability: 99.9% excluding upstream model outages
- Schema compliance for constrained requests: at least 99%
- Gateway overhead excluding provider time: p99 below 50 ms
- Automatic rollback state transition: below 1 second
- Cross-tenant cache leakage: zero tolerated events

## Provider outage

Symptoms: rising `provider_unavailable`, route circuit opens, fallback count increases.

1. Group failures by route and provider.
2. Confirm the original request contract admits a fallback.
3. Check whether failures began after credential, endpoint, model, or catalog changes.
4. Leave the circuit open while testing one half-open probe.
5. Disable the route in a reviewed catalog deployment if recovery is not imminent.

Do not increase retries globally. It amplifies overload and consumes the caller deadline.

## Schema regression

Symptoms: lower schema compliance with normal HTTP success.

1. Compare failures by model revision, prompt version, schema hash, and canary assignment.
2. Re-run exact cases offline against baseline and candidate.
3. Confirm the provider route declares native structured-output support.
4. Roll back the release if the live threshold is violated.
5. Add each observed malformed output to the regression dataset.

## Tail-latency regression

Symptoms: p50 stable, p99 exceeds gate, TTFT or total latency diverges.

1. Separate queueing, connection, TTFT, and generation time.
2. Compare input/output token distributions before blaming the provider.
3. Inspect shadow/canary traffic for resource contention.
4. Reduce concurrency or output budget; do not hide the issue with a larger caller timeout.

## Cost spike

1. Inspect cost per successful request rather than total spend alone.
2. Check cache-hit changes, fallback duplication, prompt growth, output-token growth, and route mix.
3. Verify catalog prices and token accounting.
4. Tighten per-request cost budgets and disable an unexpectedly expensive route if necessary.

## Manual rollback

```bash
curl -s -X POST http://127.0.0.1:8000/v1/control/releases/RELEASE_ID/rollback \
  -H "Authorization: Bearer $AEGIS_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"incident-identifier"}'
```

After rollback, verify assignment, success rate, p99, schema compliance, and that background shadow
traffic is no longer attached to the rolled-back release.

## Backup and restore

For the laptop implementation, stop writes and snapshot the SQLite database plus route catalog.
Production PostgreSQL must use tested point-in-time recovery. A restore drill is incomplete until
artifact hashes, release pointers, evaluation runs, and request evidence are reconciled.
