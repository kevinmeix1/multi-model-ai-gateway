"""FastAPI surface for generation, compatibility, and control-plane operations."""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import Field

from aegis_gateway.config import Settings
from aegis_gateway.domain import (
    ArtifactKind,
    GatewayRequest,
    Message,
    Release,
    StrictModel,
)
from aegis_gateway.errors import AegisError
from aegis_gateway.runtime import Runtime, create_runtime


class ArtifactCreate(StrictModel):
    kind: ArtifactKind
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    content: dict[str, Any]


class EvaluationRequest(StrictModel):
    dataset_name: str
    dataset_version: str | None = None
    route_id: str


class CanaryStartRequest(StrictModel):
    dataset_name: str
    dataset_version: str | None = None


class ManualRollbackRequest(StrictModel):
    reason: str = Field(default="manual", min_length=1, max_length=500)


class OpenAICompatRequest(StrictModel):
    model: str = "auto"
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int = Field(default=512, ge=1, le=131_072)
    response_format: dict[str, Any] | None = None
    user: str | None = None


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or create_runtime(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.initialize()
        yield
        await runtime.aclose()

    app = FastAPI(
        title="Aegis AI Gateway",
        version="0.1.0",
        description="Policy-first multi-model gateway and evaluation control plane",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.exception_handler(AegisError)
    async def aegis_error_handler(_request: Request, exc: AegisError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}},
        )

    async def require_admin(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = runtime.settings.admin_token.get_secret_value()
        presented = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=401, detail="invalid control-plane credential")

    admin = Depends(require_admin)

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> dict[str, Any]:
        return {
            "status": "ready",
            "providers": await runtime.service.providers.health(),
            "enabled_routes": [
                route.id for route in runtime.service.router.routes if route.enabled
            ],
        }

    @app.post("/v1/generate", tags=["data-plane"])
    async def generate(body: GatewayRequest) -> Response:
        if not body.stream:
            result = await runtime.service.generate(body)
            return JSONResponse(result.model_dump(mode="json"))
        return StreamingResponse(
            _aegis_stream(runtime, body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/chat/completions", tags=["compatibility"])
    async def openai_compat(
        body: OpenAICompatRequest,
        x_aegis_tenant: Annotated[str, Header(min_length=1)] = "default",
    ) -> Response:
        schema, schema_name = _compat_schema(body.response_format)
        gateway_request = GatewayRequest(
            tenant_id=x_aegis_tenant,
            user_id=body.user,
            messages=body.messages,
            stream=body.stream,
            temperature=body.temperature,
            max_output_tokens=body.max_tokens,
            response_schema=schema,
            schema_name=schema_name,
        )
        forced_route = None if body.model == "auto" else body.model
        if body.stream:
            return StreamingResponse(
                _openai_compat_stream(runtime, gateway_request, forced_route),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        result = await runtime.service.generate(gateway_request, forced_route_id=forced_route)
        return JSONResponse(
            {
                "id": f"chatcmpl-{result.request_id}",
                "object": "chat.completion",
                "created": int(result.created_at.timestamp()),
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": result.usage.input_tokens,
                    "completion_tokens": result.usage.output_tokens,
                    "total_tokens": result.usage.input_tokens + result.usage.output_tokens,
                },
                "aegis": {
                    "route_id": result.route_id,
                    "cost_usd": result.usage.cost_usd,
                    "latency_ms": result.latency_ms,
                    "cache_hit": result.cache_hit,
                },
            }
        )

    @app.get("/metrics", tags=["observability"])
    async def prometheus() -> Response:
        return Response(
            runtime.service.telemetry.render_prometheus(),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/console", response_class=HTMLResponse, tags=["observability"])
    async def console() -> str:
        if not runtime.settings.enable_console:
            raise HTTPException(status_code=404)
        return _console_html()

    @app.get("/v1/control/routes", dependencies=[admin], tags=["control-plane"])
    async def routes() -> list[dict[str, Any]]:
        return [route.model_dump(mode="json") for route in runtime.service.router.routes]

    @app.get("/v1/control/providers/health", dependencies=[admin], tags=["control-plane"])
    async def provider_health() -> dict[str, bool]:
        return await runtime.service.providers.health()

    @app.get("/v1/control/circuits", dependencies=[admin], tags=["control-plane"])
    async def circuits() -> list[dict[str, Any]]:
        return [
            {
                "route_id": item.route_id,
                "state": item.state,
                "consecutive_failures": item.consecutive_failures,
                "opened_at_monotonic": item.opened_at,
            }
            for item in await runtime.service.circuits.snapshots()
        ]

    @app.delete("/v1/control/cache", dependencies=[admin], tags=["control-plane"])
    async def clear_cache() -> dict[str, int]:
        return {"entries_removed": await runtime.service.cache.clear()}

    @app.post("/v1/control/artifacts", dependencies=[admin], tags=["registries"])
    async def create_artifact(body: ArtifactCreate) -> dict[str, Any]:
        artifact = await runtime.artifacts.put(
            kind=body.kind, name=body.name, version=body.version, content=body.content
        )
        return artifact.model_dump(mode="json")

    @app.get("/v1/control/artifacts", dependencies=[admin], tags=["registries"])
    async def list_artifacts(
        kind: Annotated[ArtifactKind | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in await runtime.artifacts.list(kind=kind)]

    @app.post("/v1/control/releases", dependencies=[admin], tags=["releases"])
    async def create_release(body: Release) -> dict[str, Any]:
        runtime.service.router.get_route(body.baseline_route_id)
        runtime.service.router.get_route(body.candidate_route_id)
        return (await runtime.release_registry.create(body)).model_dump(mode="json")

    @app.get("/v1/control/releases", dependencies=[admin], tags=["releases"])
    async def list_releases() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in await runtime.release_registry.list()]

    @app.post(
        "/v1/control/releases/{release_id}/start-canary",
        dependencies=[admin],
        tags=["releases"],
    )
    async def start_canary(release_id: str, body: CanaryStartRequest) -> dict[str, Any]:
        release = await runtime.release_registry.get(release_id)
        run = await runtime.evaluations.run(
            dataset_name=body.dataset_name,
            dataset_version=body.dataset_version,
            route_id=release.candidate_route_id,
        )
        runtime.evaluations.enforce_release_gate(release, run)
        updated = await runtime.release_manager.start_canary(release_id)
        return {
            "release": updated.model_dump(mode="json"),
            "evaluation": run.model_dump(mode="json"),
        }

    @app.post(
        "/v1/control/releases/{release_id}/promote",
        dependencies=[admin],
        tags=["releases"],
    )
    async def promote(release_id: str) -> dict[str, Any]:
        return (await runtime.release_manager.promote(release_id)).model_dump(mode="json")

    @app.post(
        "/v1/control/releases/{release_id}/rollback",
        dependencies=[admin],
        tags=["releases"],
    )
    async def rollback(release_id: str, body: ManualRollbackRequest) -> dict[str, Any]:
        decision = await runtime.release_manager.rollback(release_id, body.reason)
        return {
            "rolled_back": decision.rolled_back,
            "reason": decision.reason,
            "duration_ms": decision.duration_ms,
        }

    @app.post(
        "/v1/control/releases/{release_id}/assess",
        dependencies=[admin],
        tags=["releases"],
    )
    async def assess(release_id: str) -> dict[str, Any]:
        decision = await runtime.release_manager.assess(release_id)
        return {
            "rolled_back": decision.rolled_back,
            "reason": decision.reason,
            "duration_ms": decision.duration_ms,
        }

    @app.post("/v1/control/evaluations", dependencies=[admin], tags=["evaluations"])
    async def run_evaluation(body: EvaluationRequest) -> dict[str, Any]:
        return (
            await runtime.evaluations.run(
                dataset_name=body.dataset_name,
                dataset_version=body.dataset_version,
                route_id=body.route_id,
            )
        ).model_dump(mode="json")

    @app.get("/v1/control/evaluations", dependencies=[admin], tags=["evaluations"])
    async def list_evaluations() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in await runtime.evidence.list_evaluations()]

    @app.get("/v1/control/metrics/summary", dependencies=[admin], tags=["observability"])
    async def metric_summary(
        route_id: str | None = None,
        release_id: str | None = None,
        canary: bool | None = None,
    ) -> dict[str, Any]:
        return (
            await runtime.evidence.summary(route_id=route_id, release_id=release_id, canary=canary)
        ).model_dump(mode="json")

    @app.get("/v1/control/metrics/recent", dependencies=[admin], tags=["observability"])
    async def recent_metrics(
        limit: int = Query(default=100, ge=1, le=10_000),
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json") for item in await runtime.evidence.metrics(limit=limit)
        ]

    return app


async def _aegis_stream(runtime: Runtime, body: GatewayRequest) -> AsyncIterator[str]:
    try:
        async for event in runtime.service.stream(body):
            yield _sse(event.type, event.model_dump(mode="json"))
    except AegisError as exc:
        yield _sse("error", {"error": {"code": exc.code, "message": str(exc)}})


async def _openai_compat_stream(
    runtime: Runtime, body: GatewayRequest, forced_route: str | None
) -> AsyncIterator[str]:
    try:
        async for event in runtime.service.stream(body, forced_route_id=forced_route):
            if event.type == "delta":
                chunk = {
                    "id": f"chatcmpl-{body.request_id}",
                    "object": "chat.completion.chunk",
                    "model": event.model,
                    "choices": [
                        {"index": 0, "delta": {"content": event.delta}, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
            elif event.type == "done":
                final = {
                    "id": f"chatcmpl-{body.request_id}",
                    "object": "chat.completion.chunk",
                    "model": event.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "aegis": {"route_id": event.route_id},
                }
                yield f"data: {json.dumps(final, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
    except AegisError as exc:
        yield f"data: {json.dumps({'error': {'code': exc.code, 'message': str(exc)}})}\n\n"
        yield "data: [DONE]\n\n"


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _compat_schema(response_format: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    if not response_format or response_format.get("type") != "json_schema":
        return None, "gateway_response"
    specification = response_format.get("json_schema") or {}
    schema = specification.get("schema")
    if not isinstance(schema, dict):
        raise HTTPException(
            status_code=422, detail="response_format.json_schema.schema is required"
        )
    return schema, str(specification.get("name", "gateway_response"))


def _console_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Aegis Control Plane</title><style>
body{font:15px system-ui;background:#09111f;color:#dce7f5;margin:0;padding:32px}main{max-width:1100px;margin:auto}
h1{font-size:30px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.card{background:#121f33;border:1px solid #243956;border-radius:12px;padding:18px}.value{font-size:27px;color:#63d4b5}
input,button{background:#121f33;color:#fff;border:1px solid #355070;border-radius:7px;padding:9px}button{cursor:pointer}
pre{white-space:pre-wrap;background:#07101d;padding:16px;border-radius:10px;overflow:auto}.muted{color:#94a8c2}
</style></head><body><main><h1>Aegis AI Gateway</h1><p class="muted">Evaluation and routing control plane</p>
<p><input id="token" type="password" placeholder="Admin token"><button onclick="load()">Refresh</button></p>
<section class="grid" id="cards"></section><h2>Current evidence</h2><pre id="raw">Enter the admin token.</pre>
<script>
async function load(){const token=document.getElementById('token').value;const h={Authorization:'Bearer '+token};
const [m,r,c]=await Promise.all(['/v1/control/metrics/summary','/v1/control/releases','/v1/control/circuits'].map(u=>fetch(u,{headers:h})));
if(!m.ok){document.getElementById('raw').textContent='Authorization failed';return}const metrics=await m.json();
const cards=[['Requests',metrics.requests],['Success',(100*metrics.success_rate).toFixed(1)+'%'],['Schema',metrics.schema_compliance===null?'n/a':(100*metrics.schema_compliance).toFixed(1)+'%'],['Cache hit',(100*metrics.cache_hit_rate).toFixed(1)+'%'],['p99',metrics.p99_latency_ms===null?'n/a':metrics.p99_latency_ms.toFixed(1)+' ms'],['Cost/success',metrics.mean_cost_per_success_usd===null?'n/a':'$'+metrics.mean_cost_per_success_usd.toFixed(5)]];
document.getElementById('cards').innerHTML=cards.map(x=>`<div class="card"><div class="muted">${x[0]}</div><div class="value">${x[1]}</div></div>`).join('');
document.getElementById('raw').textContent=JSON.stringify({metrics,releases:await r.json(),circuits:await c.json()},null,2)}
</script></main></body></html>"""


app = create_app()
