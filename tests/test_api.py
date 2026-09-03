from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import HTTPException

from aegis_gateway.api.app import _compat_schema, _sse, create_app
from aegis_gateway.runtime import Runtime


async def client_for(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://aegis.test",
    ) as client:
        yield client


def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token"}


async def test_health_observability_console_and_admin_auth(runtime: Runtime) -> None:
    async for client in client_for(runtime):
        live = await client.get("/health/live")
        assert live.json() == {"status": "alive"}
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert "mock-primary" in ready.json()["enabled_routes"]
        assert ready.json()["providers"]["mock"] is True
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert "aegis_requests_total" in metrics.text
        assert "Aegis AI Gateway" in (await client.get("/console")).text

        assert (await client.get("/v1/control/routes")).status_code == 401
        assert (
            await client.get("/v1/control/routes", headers={"Authorization": "Basic wrong"})
        ).status_code == 401
        routes = await client.get("/v1/control/routes", headers=admin_headers())
        assert routes.status_code == 200
        providers = await client.get("/v1/control/providers/health", headers=admin_headers())
        assert providers.status_code == 200
        assert (await client.get("/v1/control/circuits", headers=admin_headers())).json() == []
        cleared = await client.delete("/v1/control/cache", headers=admin_headers())
        assert cleared.json() == {"entries_removed": 0}

        runtime.settings.enable_console = False
        assert (await client.get("/console")).status_code == 404
        runtime.settings.enable_console = True


async def test_native_generation_streaming_and_error_envelope(runtime: Runtime) -> None:
    async for client in client_for(runtime):
        payload = {
            "tenant_id": "api-tenant",
            "request_id": "api-request-0001",
            "messages": [{"role": "user", "content": "hello native"}],
        }
        response = await client.post("/v1/generate", json=payload)
        assert response.status_code == 200
        assert response.json()["route_id"] == "mock-primary"
        assert response.json()["text"].endswith("hello native")

        streamed = await client.post(
            "/v1/generate",
            json={
                **payload,
                "request_id": "api-request-0002",
                "stream": True,
                "messages": [{"role": "user", "content": "stream native"}],
            },
        )
        assert streamed.status_code == 200
        assert "event: start" in streamed.text
        assert "event: delta" in streamed.text
        assert "event: done" in streamed.text

        no_route = await client.post(
            "/v1/generate",
            json={
                **payload,
                "request_id": "api-request-0003",
                "allowed_regions": ["moon"],
            },
        )
        assert no_route.status_code == 422
        assert no_route.json()["error"]["code"] == "no_eligible_route"


async def test_openai_compat_complete_stream_and_structured_output(runtime: Runtime) -> None:
    async for client in client_for(runtime):
        headers = {"X-Aegis-Tenant": "compat-tenant"}
        complete = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "mock-canary",
                "messages": [{"role": "user", "content": "hello compat"}],
                "user": "user-1",
            },
        )
        assert complete.status_code == 200
        body = complete.json()
        assert body["object"] == "chat.completion"
        assert body["aegis"]["route_id"] == "mock-canary"
        assert body["usage"]["total_tokens"] > 0

        structured = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "health"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "health",
                        "schema": {
                            "type": "object",
                            "properties": {"status": {"const": "ok"}},
                            "required": ["status"],
                        },
                    },
                },
            },
        )
        assert structured.status_code == 200
        assert structured.json()["choices"][0]["message"]["content"] == '{"status": "ok"}'

        stream = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "stream compat"}],
            },
        )
        assert stream.status_code == 200
        assert '"object":"chat.completion.chunk"' in stream.text
        assert "data: [DONE]" in stream.text

        invalid = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "messages": [{"role": "user", "content": "bad schema"}],
                "response_format": {"type": "json_schema", "json_schema": {}},
            },
        )
        assert invalid.status_code == 422


async def test_control_plane_artifact_evaluation_and_release_workflow(runtime: Runtime) -> None:
    headers = admin_headers()
    async for client in client_for(runtime):
        dataset = await client.post(
            "/v1/control/artifacts",
            headers=headers,
            json={
                "kind": "dataset",
                "name": "api-smoke",
                "version": "1",
                "content": {
                    "cases": [
                        {
                            "id": "case-1",
                            "messages": [{"role": "user", "content": "release test"}],
                            "expected_contains": ["release test"],
                        }
                    ]
                },
            },
        )
        assert dataset.status_code == 200
        listed = await client.get("/v1/control/artifacts?kind=dataset", headers=headers)
        assert len(listed.json()) == 1

        release_body = {
            "id": "api-release",
            "name": "default",
            "baseline_route_id": "mock-primary",
            "candidate_route_id": "mock-canary",
            "canary_percent": 100,
            "shadow_percent": 0,
            "min_quality_score": 1,
            "min_schema_compliance": 1,
            "max_error_rate": 0,
            "max_p99_latency_ms": 1000,
            "min_canary_samples": 1,
        }
        created = await client.post("/v1/control/releases", headers=headers, json=release_body)
        assert created.status_code == 200
        assert len((await client.get("/v1/control/releases", headers=headers)).json()) == 1

        direct_eval = await client.post(
            "/v1/control/evaluations",
            headers=headers,
            json={
                "dataset_name": "api-smoke",
                "dataset_version": "1",
                "route_id": "mock-canary",
            },
        )
        assert direct_eval.json()["pass_rate"] == 1
        assert len((await client.get("/v1/control/evaluations", headers=headers)).json()) == 1

        started = await client.post(
            "/v1/control/releases/api-release/start-canary",
            headers=headers,
            json={"dataset_name": "api-smoke", "dataset_version": "1"},
        )
        assert started.status_code == 200
        assert started.json()["release"]["state"] == "candidate"
        promoted = await client.post("/v1/control/releases/api-release/promote", headers=headers)
        assert promoted.json()["state"] == "active"
        assessed = await client.post("/v1/control/releases/api-release/assess", headers=headers)
        assert assessed.json()["rolled_back"] is False
        rolled_back = await client.post(
            "/v1/control/releases/api-release/rollback",
            headers=headers,
            json={"reason": "test operator"},
        )
        assert rolled_back.json()["rolled_back"] is True

        await client.post(
            "/v1/generate",
            json={
                "tenant_id": "metric-tenant",
                "messages": [{"role": "user", "content": "produce evidence"}],
            },
        )
        summary = await client.get("/v1/control/metrics/summary", headers=headers)
        assert summary.json()["requests"] >= 1
        recent = await client.get("/v1/control/metrics/recent?limit=1", headers=headers)
        assert len(recent.json()) == 1


def test_compat_schema_and_sse_helpers() -> None:
    assert _compat_schema(None) == (None, "gateway_response")
    assert _compat_schema({"type": "text"}) == (None, "gateway_response")
    with pytest.raises(HTTPException):
        _compat_schema({"type": "json_schema", "json_schema": {}})
    assert _sse("delta", {"text": "hello"}) == 'event: delta\ndata: {"text":"hello"}\n\n'
