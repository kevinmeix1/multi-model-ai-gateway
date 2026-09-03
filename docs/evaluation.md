# Evaluation and release methodology

## Dataset contract

An immutable dataset artifact contains a `cases` array. Each case provides messages plus any
combination of an exact expected response, required substrings, a JSON Schema, and diagnostic tags.
Evaluation bypasses cache, disables shadow execution, forces one route, and stores one result per
case.

Example:

```json
{
  "cases": [
    {
      "id": "structured-001",
      "messages": [{"role": "user", "content": "Return a health status."}],
      "response_schema": {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
        "additionalProperties": false
      },
      "tags": ["schema", "smoke"]
    }
  ]
}
```

## Metrics

| Metric | Definition | Failure it detects |
|---|---|---|
| Schema compliance | Valid structured outputs / schema-constrained outputs | Contract drift |
| Cost per success | Sum of actual estimated cost / successful requests | Cheap failures hiding cost |
| TTFT | First content delta time minus request start | Poor perceived latency |
| p99 latency | Nearest-rank 99th percentile of end-to-end latency | Tail amplification |
| Routing regret | Best eligible utility minus selected utility | Policy/canary opportunity cost |
| Failover success | Successful requests with fallback / all requests with fallback | Fragile degradation |
| Rollback time | State-transition completion minus rollback decision time | Slow mitigation |

Provider prices and route quality priors are configuration data. They must be updated from measured
evidence; the example catalog is not a pricing authority.

## Release protocol

1. Register immutable prompt, dataset, model-catalog, and policy artifacts.
2. Run the baseline and candidate on the same dataset.
3. Inspect per-case deltas, not only aggregate averages.
4. Reject candidates violating quality, schema, error-rate, or p99 thresholds.
5. Start shadow traffic to detect integration failures without user impact.
6. Start deterministic canary assignment.
7. Assess live error rate, schema compliance, and p99 after the minimum sample count.
8. Promote explicitly or rollback automatically.

The small demo gate is deterministic. Real releases should add paired bootstrap intervals, judge
agreement audits, stratification by task and tenant, safety hard stops, and power calculations.

## Avoiding evaluator leakage

- Keep final holdout cases inaccessible to prompt authors.
- Version evaluator prompts and model revisions as artifacts.
- Do not use one model judge as ground truth; calibrate against human labels.
- Separate retrieval, tool, schema, safety, and final-task failures.
- Preserve raw outputs and trace identifiers needed for adjudication.
