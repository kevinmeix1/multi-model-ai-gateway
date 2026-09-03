from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from aegis_gateway.domain import (
    ArtifactKind,
    ArtifactRef,
    CacheMode,
    GatewayRequest,
    ModelRoute,
    ProviderResult,
    ProviderStreamEvent,
    Release,
)
from aegis_gateway.errors import ProviderError, SchemaViolationError, StreamInterruptedError
from aegis_gateway.providers.mock import MockAdapter
from aegis_gateway.runtime import Runtime


def mock(runtime: Runtime) -> MockAdapter:
    adapter = runtime.service.providers.get("mock")
    assert isinstance(adapter, MockAdapter)
    return adapter


async def test_generate_records_metrics_and_serves_zero_cost_cache_hit(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    request = request_factory()
    first = await runtime.service.generate(request)
    second = await runtime.service.generate(
        request.model_copy(update={"request_id": "request-0002"})
    )
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.usage.cost_usd == 0
    assert second.usage.input_tokens == 0
    assert second.request_id == "request-0002"
    assert mock(runtime).calls == 1
    summary = await runtime.evidence.summary()
    assert summary.requests == 2
    assert summary.cache_hit_rate == 0.5
    assert b"aegis_cache_hits_total 1.0" in runtime.service.telemetry.render_prometheus()


async def test_cache_bypass_and_refresh_semantics(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    bypass = request_factory(cache_mode=CacheMode.BYPASS)
    await runtime.service.generate(bypass)
    await runtime.service.generate(bypass.model_copy(update={"request_id": "request-0002"}))
    assert mock(runtime).calls == 2
    assert await runtime.service.cache.size() == 0

    refresh = request_factory(
        request_id="request-0003",
        cache_mode=CacheMode.REFRESH,
        messages=[{"role": "user", "content": "refresh me"}],
    )
    assert (await runtime.service.generate(refresh)).cache_hit is False
    default = refresh.model_copy(
        update={"request_id": "request-0004", "cache_mode": CacheMode.DEFAULT}
    )
    assert (await runtime.service.generate(default)).cache_hit is True


async def test_provider_failure_falls_back_only_to_compatible_route(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    response = await runtime.service.generate(
        request_factory(
            metadata={
                "mock_failure": "unavailable",
                "mock_failure_route": "mock-primary",
            }
        )
    )
    assert response.route_id == "mock-canary"
    assert response.fallback_count == 1
    assert response.routing_regret > 0
    assert (await runtime.evidence.summary()).failover_success_rate == 1


async def test_schema_failure_falls_back_and_all_failures_are_evidenced(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = mock(runtime)
    original = adapter.complete

    async def invalid_then_valid(request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        if route.id == "mock-primary":
            return ProviderResult(
                text="not-json",
                input_tokens=1,
                output_tokens=1,
                ttft_ms=1,
                latency_ms=1,
                raw_model=route.model,
            )
        return await original(request, route)

    monkeypatch.setattr(adapter, "complete", invalid_then_valid)
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
        "additionalProperties": False,
    }
    response = await runtime.service.generate(request_factory(response_schema=schema))
    assert response.route_id == "mock-canary"
    assert response.schema_valid is True
    assert response.parsed == {"status": "example"}

    async def always_invalid(_request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        return ProviderResult(
            text="{}",
            input_tokens=1,
            output_tokens=1,
            ttft_ms=1,
            latency_ms=1,
            raw_model=route.model,
        )

    monkeypatch.setattr(adapter, "complete", always_invalid)
    with pytest.raises(SchemaViolationError):
        await runtime.service.generate(
            request_factory(
                request_id="request-fail",
                response_schema=schema,
                cache_mode=CacheMode.BYPASS,
            )
        )
    failed = await runtime.evidence.metrics(limit=1)
    assert failed[0].success is False
    assert failed[0].error_code == "schema_violation"


async def test_noncompatible_provider_request_stops_fallback(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = mock(runtime)

    async def invalid(_request: GatewayRequest, _route: ModelRoute) -> ProviderResult:
        raise ProviderError(
            "bad request",
            provider="mock",
            retryable=False,
            code="invalid_provider_request",
        )

    monkeypatch.setattr(adapter, "complete", invalid)
    with pytest.raises(ProviderError) as captured:
        await runtime.service.generate(request_factory())
    assert captured.value.code == "invalid_provider_request"
    assert adapter.calls == 0
    evidence = await runtime.evidence.metrics()
    assert evidence[0].fallback_count == 1


async def test_prompt_registry_renders_or_rejects_bad_artifacts(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await runtime.artifacts.put(
        kind=ArtifactKind.PROMPT,
        name="audience",
        version="1",
        content={"template": "Write for {{ audience }} about {{ topic }}."},
    )
    observed: dict[str, Any] = {}
    adapter = mock(runtime)
    original = adapter.complete

    async def capture(request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        observed["messages"] = request.messages
        return await original(request, route)

    monkeypatch.setattr(adapter, "complete", capture)
    request = request_factory(
        prompt=ArtifactRef(name="audience", version="1"),
        prompt_variables={"audience": "architects", "topic": "routing"},
    )
    await runtime.service.generate(request)
    messages = observed["messages"]
    assert messages[0].role == "developer"
    assert messages[0].content == "Write for architects about routing."

    with pytest.raises(ValueError, match="missing prompt variables"):
        await runtime.service.generate(
            request_factory(
                request_id="request-0002",
                prompt=ArtifactRef(name="audience"),
                prompt_variables={"audience": "architects"},
            )
        )
    await runtime.artifacts.put(
        kind=ArtifactKind.PROMPT,
        name="broken",
        version="1",
        content={"template": ""},
    )
    with pytest.raises(ValueError, match="non-empty"):
        await runtime.service.generate(
            request_factory(request_id="request-0003", prompt=ArtifactRef(name="broken"))
        )


async def test_stream_normalizes_events_records_usage_and_caches(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    request = request_factory(stream=True)
    events = [event async for event in runtime.service.stream(request)]
    assert events[0].type == "start"
    assert any(event.type == "delta" for event in events)
    assert events[-1].type == "done"
    response = events[-1].response
    assert response is not None
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0

    cached = [
        event
        async for event in runtime.service.stream(
            request.model_copy(update={"request_id": "request-0002"})
        )
    ]
    assert [event.type for event in cached] == ["start", "delta", "done"]
    assert cached[-1].response is not None
    assert cached[-1].response.cache_hit is True


async def test_stream_falls_back_before_output_but_never_after_output(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre_output = request_factory(
        stream=True,
        metadata={"mock_failure": "timeout", "mock_failure_route": "mock-primary"},
    )
    events = [event async for event in runtime.service.stream(pre_output)]
    assert events[-1].response is not None
    assert events[-1].response.route_id == "mock-canary"
    assert events[-1].response.fallback_count == 1

    adapter = mock(runtime)

    async def interrupted(
        _request: GatewayRequest, route: ModelRoute
    ) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="start")
        yield ProviderStreamEvent(type="delta", delta="partial")
        raise ProviderError("lost", provider=route.provider)

    monkeypatch.setattr(adapter, "stream", interrupted)
    with pytest.raises(StreamInterruptedError):
        _ = [
            event
            async for event in runtime.service.stream(
                request_factory(
                    request_id="request-interrupted",
                    stream=True,
                    cache_mode=CacheMode.BYPASS,
                )
            )
        ]
    evidence = await runtime.evidence.metrics(limit=1)
    assert evidence[0].error_code == "stream_interrupted"


async def test_stream_schema_violation_after_output_is_not_retried(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = mock(runtime)

    async def invalid_stream(
        _request: GatewayRequest, _route: ModelRoute
    ) -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="start")
        yield ProviderStreamEvent(type="delta", delta="not-json")
        yield ProviderStreamEvent(type="done")

    monkeypatch.setattr(adapter, "stream", invalid_stream)
    with pytest.raises(SchemaViolationError):
        _ = [
            event
            async for event in runtime.service.stream(
                request_factory(
                    stream=True,
                    response_schema={"type": "object"},
                    cache_mode=CacheMode.BYPASS,
                )
            )
        ]


async def test_shadow_traffic_is_isolated_and_recorded(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    created = await runtime.release_registry.create(
        Release(
            id="shadow-release",
            name="default",
            baseline_route_id="mock-primary",
            candidate_route_id="mock-canary",
            canary_percent=0,
            shadow_percent=100,
        )
    )
    await runtime.release_manager.start_canary(created.id)
    response = await runtime.service.generate(request_factory())
    assert response.route_id == "mock-primary"
    await asyncio.gather(*runtime.service._background)
    metrics = await runtime.evidence.metrics(release_id=created.id)
    assert len(metrics) == 2
    assert {item.shadow for item in metrics} == {False, True}
    assert next(item for item in metrics if item.shadow).request_id.endswith("-shadow")
