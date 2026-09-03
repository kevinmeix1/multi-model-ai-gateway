# API reference

The service exposes a native Aegis API, a narrow OpenAI-compatible endpoint, health/metrics routes,
and an authenticated control plane. Interactive OpenAPI documentation is available at `/docs` while
the service is running.

Examples assume `http://127.0.0.1:8000`.

## Conventions

- JSON requests use `Content-Type: application/json`.
- Native streaming uses server-sent events (SSE) with named event types.
- Compatibility streaming uses `data:` frames and a `[DONE]` sentinel.
- Timestamps are UTC ISO 8601 in native responses.
- Monetary values are estimated US dollars based on route catalogue prices.
- Unknown JSON fields are rejected.
- Control routes require `Authorization: Bearer <AEGIS_ADMIN_TOKEN>`.

## Native generation

### `POST /v1/generate`

Minimal request:

```json
{
  "tenant_id": "payments-api",
  "messages": [
    {"role": "user", "content": "Summarize this incident in three bullets."}
  ]
}
```

Full request shape:

| Field | Type | Default | Notes |
|---|---|---|---|
| `tenant_id` | string | required | 1–128 characters, letters/numbers/`_.-` |
| `request_id` | string | generated UUID | 8–128 characters; correlation, not authorization |
| `user_id` | string/null | null | Opaque external identifier, max 256 |
| `messages` | array | required | 1–256 messages |
| `prompt` | object/null | null | Artifact `{name, version?}` |
| `prompt_variables` | object | `{}` | String substitutions for the prompt template |
| `required_capabilities` | string set | `text` | Additional `tools`/`vision`; stream/schema inferred |
| `data_classification` | enum | `internal` | `public`, `internal`, `confidential`, `restricted` |
| `privacy_mode` | enum | `standard` | `standard`, `zero_retention`, `local_only` |
| `allowed_regions` | string set | empty | Must intersect route regions when supplied |
| `max_cost_usd` | number | `0.05` | 0–1000, hard predicted-cost ceiling |
| `max_latency_ms` | integer | `10000` | 50–600000, route latency ceiling |
| `max_output_tokens` | integer | `512` | 1–131072 |
| `temperature` | number/null | null | 0–2; provider/model support varies |
| `stream` | boolean | false | Requires streaming-capable route |
| `response_schema` | object/null | null | JSON Schema for structured output |
| `schema_name` | string | `gateway_response` | Provider schema label |
| `cache_mode` | enum | `default` | `default`, `bypass`, `refresh` |
| `shadow_enabled` | boolean | true | Allows release policy to shadow this request |
| `metadata` | string map | `{}` | Operational metadata; mock adapter also supports test faults |

Message shape:

```json
{
  "role": "system | developer | user | assistant | tool",
  "content": "non-empty text",
  "name": "optional name"
}
```

The provider adapters do not yet normalize tool-result or image content. A route advertising those
capabilities does not by itself extend this API shape.

### Complete response

```json
{
  "request_id": "4c4e4ca2-9ea5-4108-90d6-a2c301b3b20a",
  "route_id": "mock-primary",
  "provider": "mock",
  "model": "deterministic-v1",
  "text": "Deterministic response [2a75d1e8]: Summarize this incident.",
  "parsed": null,
  "schema_valid": null,
  "usage": {
    "input_tokens": 12,
    "output_tokens": 16,
    "cost_usd": 0.0
  },
  "ttft_ms": 1.2,
  "latency_ms": 2.8,
  "cache_hit": false,
  "fallback_count": 0,
  "routing_regret": 0.0,
  "created_at": "2026-09-04T12:00:00Z"
}
```

`schema_valid` is null for plain text, true for accepted structured output, and terminal schema
failure is returned as an error rather than a response with false. `parsed` contains the decoded JSON
value for successful structured output.

On a cache hit, usage and cost are zero, TTFT/latency describe lookup time, and route/provider/model
retain the provenance of the cached response.

### Structured output request

```bash
curl -sS http://127.0.0.1:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "status-page",
    "messages": [{"role":"user","content":"Return current health."}],
    "response_schema": {
      "type": "object",
      "properties": {
        "status": {"type":"string","enum":["healthy","degraded"]},
        "summary": {"type":"string"}
      },
      "required": ["status","summary"],
      "additionalProperties": false
    },
    "schema_name": "service_health"
  }'
```

The schema is sent through a provider-native structured-output field where supported and validated
again in the gateway with `jsonschema`.

## Native streaming

Set `stream=true` on the same endpoint:

```bash
curl -N http://127.0.0.1:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id":"stream-demo",
    "stream":true,
    "messages":[{"role":"user","content":"Explain routing regret."}]
  }'
```

Event sequence:

```text
event: start
data: {"type":"start","request_id":"...","route_id":"mock-primary",...}

event: delta
data: {"type":"delta","request_id":"...","delta":"Determi",...}

event: delta
data: {"type":"delta","request_id":"...","delta":"nistic ",...}

event: done
data: {"type":"done","request_id":"...","response":{...},...}
```

Native event fields:

| Field | Meaning |
|---|---|
| `type` | `start`, `delta`, `done`, or `error` |
| `request_id` | Gateway request ID |
| `route_id` | Committed route for the event |
| `provider` | Adapter key |
| `model` | Route/provider model identifier |
| `delta` | Text fragment on delta events |
| `response` | Final `GatewayResponse` on done |
| `error_code` | Typed code when represented as a model event |

The API handler emits an `event: error` payload if a typed Aegis exception occurs during iteration:

```text
event: error
data: {"error":{"code":"stream_interrupted","message":"..."}}
```

There is no hidden provider switch after the first content delta.

## OpenAI-compatible chat completions

### `POST /v1/chat/completions`

This endpoint supports migration of basic text chat-completions clients. It is not full upstream API
compatibility.

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Aegis-Tenant: support' \
  -d '{
    "model":"auto",
    "messages":[{"role":"user","content":"Hello"}],
    "max_tokens":256,
    "temperature":0.2,
    "stream":false,
    "user":"opaque-user-id"
  }'
```

Compatibility fields:

| Field | Behavior |
|---|---|
| `model` | `auto` invokes routing; another value is treated as an Aegis route ID, not raw model name |
| `messages` | Same text-only message model as native API |
| `stream` | Returns data-only SSE chunks and `[DONE]` |
| `temperature` | Passed through normalized request when provider supports it |
| `max_tokens` | Maps to `max_output_tokens` |
| `response_format` | Supports `type=json_schema` with nested name/schema |
| `user` | Maps to `user_id` |
| `X-Aegis-Tenant` | Tenant namespace; defaults to `default` in reference API |

Example response:

```json
{
  "id": "chatcmpl-<gateway-request-id>",
  "object": "chat.completion",
  "created": 1788523200,
  "model": "deterministic-v1",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 8,
    "completion_tokens": 10,
    "total_tokens": 18
  },
  "aegis": {
    "route_id": "mock-primary",
    "cost_usd": 0.0,
    "latency_ms": 2.4,
    "cache_hit": false
  }
}
```

Unsupported compatibility areas include tool calls, image/audio input, log probabilities, multiple
choices, stop sequences, seed, penalties, reasoning controls, stored conversation state, and the
complete upstream error/event schema.

### Compatibility streaming

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"...","choices":[{"index":0,"delta":{"content":"..."},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"aegis":{"route_id":"mock-primary"}}

data: [DONE]
```

Errors are emitted as a data frame followed by `[DONE]`. Clients that need Aegis-specific route and
policy details should use the native endpoint.

## Health and observability

| Method/path | Authentication | Description |
|---|---|---|
| `GET /health/live` | None | Process/event-loop liveness |
| `GET /health/ready` | None | Provider health map and enabled route IDs |
| `GET /metrics` | None | Prometheus exposition format |
| `GET /console` | None | Small control-plane viewer; it asks for admin token in-browser |

Protect `/metrics` and `/console` at ingress in production. Route names, providers, releases, and
traffic behavior can be sensitive even when prompts are absent.

## Control plane

All routes below require:

```text
Authorization: Bearer <AEGIS_ADMIN_TOKEN>
```

The reference token grants every operation. Production needs scoped RBAC and an audit log.

### Route and provider inspection

| Method/path | Result |
|---|---|
| `GET /v1/control/routes` | Validated route catalogue |
| `GET /v1/control/providers/health` | Adapter health map |
| `GET /v1/control/circuits` | Known route circuit snapshots |
| `DELETE /v1/control/cache` | Clears all local entries and returns count |

### Artifact registry

`POST /v1/control/artifacts`:

```json
{
  "kind": "prompt",
  "name": "concise-assistant",
  "version": "1.0.0",
  "content": {
    "template": "Answer for {{ audience }} in at most {{ sentences }} sentences."
  }
}
```

An identical repeat is idempotent. Different content under the same kind/name/version returns a 409
conflict.

`GET /v1/control/artifacts?kind=dataset` lists artifacts; omit `kind` for all kinds.

### Evaluations

`POST /v1/control/evaluations`:

```json
{
  "dataset_name": "gateway-smoke",
  "dataset_version": "1.0.0",
  "route_id": "mock-primary"
}
```

`GET /v1/control/evaluations` returns recent persisted runs.

### Releases

| Method/path | Body | Behavior |
|---|---|---|
| `POST /v1/control/releases` | Full `Release` | Validates route IDs and creates draft |
| `GET /v1/control/releases` | None | Lists releases by update time |
| `POST /v1/control/releases/{id}/start-canary` | dataset name/version | Runs candidate eval, enforces gate, sets candidate |
| `POST /v1/control/releases/{id}/promote` | None | Sets candidate active |
| `POST /v1/control/releases/{id}/rollback` | `{reason}` | Sets rolled back and records duration |
| `POST /v1/control/releases/{id}/assess` | None | Evaluates live canary thresholds now |

### Evidence

| Method/path | Query parameters | Result |
|---|---|---|
| `GET /v1/control/metrics/summary` | `route_id`, `release_id`, `canary` | Aggregate `MetricsSnapshot` |
| `GET /v1/control/metrics/recent` | `limit` (1–10000) | Recent request evidence rows |

The summary has no explicit time range in the reference implementation. Use unique release IDs and
move analytical queries to a time-aware store at scale.

## Error envelopes

Typed gateway errors use:

```json
{
  "error": {
    "code": "no_eligible_route",
    "message": "no route satisfies the request contract: ...",
    "retryable": false
  }
}
```

| Code | HTTP | Retryable | Typical cause |
|---|---:|---:|---|
| `no_eligible_route` | 422 | No | Every route violates at least one hard constraint |
| `rate_limit_exceeded` | 429 | Yes | Tenant token bucket empty |
| `provider_auth_error` | 502 | No | Gateway provider credential invalid/missing |
| `provider_rate_limited` | 503 | Yes | Upstream 429 |
| `provider_timeout` | 504 | Yes | Upstream timeout |
| `provider_unavailable` | 502 | Yes | Upstream 5xx |
| `invalid_provider_request` | 422 | No | Upstream non-retryable 4xx |
| `provider_transport_error` | 502 | Yes | HTTP transport fault |
| `provider_adapter_error` | 502 | No | Unexpected parsing/adapter error |
| `provider_stream_error` | 502 | Yes | Provider terminal stream event |
| `stream_interrupted` | 502 | No | Failure after content was exposed |
| `schema_violation` | 502 | Yes | Output not valid against requested schema |
| `artifact_not_found` | 404 | No | Registry lookup failed |
| `registry_conflict` | 409 | No | Immutable version already has different content |
| `evaluation_gate_failed` | 409 | No | Candidate misses release thresholds |

FastAPI/Pydantic validation errors use the framework's standard 422 detail array rather than this
envelope. Unexpected internal exceptions return the server's generic 500 behavior and should not
expose tracebacks at the public ingress.

## Idempotency and retries

The data plane does not implement an idempotency-key store. Reusing `request_id` does not prevent a
second provider call or evidence row. Client retries can therefore incur duplicate cost.

Only retry when the error is marked retryable, the operation has no external side effect, and the
caller deadline has enough time. Generate a new attempt/trace ID while retaining a separate logical
request correlation ID in a production client.

Control-plane artifact registration is content-idempotent for the same immutable key. Release state
operations do not currently require an expected state/version and can race; serialize consequential
operator actions until compare-and-swap is added.
