"""Deterministic adapter used for local demos, tests, and fault injection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from hashlib import sha256
from time import monotonic
from typing import Any

from aegis_gateway.domain import (
    GatewayRequest,
    ModelRoute,
    ProviderResult,
    ProviderStreamEvent,
)
from aegis_gateway.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from aegis_gateway.providers.base import ProviderAdapter, estimate_tokens, request_input_tokens


class MockAdapter(ProviderAdapter):
    name = "mock"

    def __init__(self, *, latency_ms: float = 1) -> None:
        self._latency_ms = latency_ms
        self.calls = 0

    async def complete(self, request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        started = monotonic()
        self.calls += 1
        await self._maybe_fail(request, route)
        await asyncio.sleep(self._latency_ms / 1000)
        text = self._render(request)
        elapsed = (monotonic() - started) * 1000
        return ProviderResult(
            provider_request_id=f"mock-{request.request_id}",
            text=text,
            input_tokens=request_input_tokens(request),
            output_tokens=estimate_tokens(text),
            finish_reason="stop",
            ttft_ms=elapsed,
            latency_ms=elapsed,
            raw_model=route.model,
        )

    async def stream(
        self, request: GatewayRequest, route: ModelRoute
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.calls += 1
        await self._maybe_fail(request, route)
        text = self._render(request)
        yield ProviderStreamEvent(type="start", provider_request_id=f"mock-{request.request_id}")
        for token in _chunks(text, 7):
            await asyncio.sleep(self._latency_ms / 1000)
            yield ProviderStreamEvent(type="delta", delta=token)
        yield ProviderStreamEvent(
            type="usage",
            input_tokens=request_input_tokens(request),
            output_tokens=estimate_tokens(text),
        )
        yield ProviderStreamEvent(type="done", finish_reason="stop")

    async def _maybe_fail(self, request: GatewayRequest, route: ModelRoute) -> None:
        failure_route = request.metadata.get("mock_failure_route")
        if failure_route is not None and failure_route != route.id:
            return
        failure = request.metadata.get("mock_failure")
        if failure == "rate_limit":
            raise ProviderRateLimitError("injected rate limit", provider=self.name)
        if failure == "timeout":
            raise ProviderTimeoutError("injected timeout", provider=self.name)
        if failure == "auth":
            raise ProviderAuthError("injected authentication failure", provider=self.name)
        if failure == "unavailable":
            raise ProviderError("injected outage", provider=self.name, retryable=True)

    @staticmethod
    def _render(request: GatewayRequest) -> str:
        if request.response_schema is not None:
            return json.dumps(_example_for_schema(request.response_schema), sort_keys=True)
        last_user = next(
            (message.content for message in reversed(request.messages) if message.role == "user"),
            request.messages[-1].content,
        )
        digest = sha256(last_user.encode()).hexdigest()[:8]
        return f"Deterministic response [{digest}]: {last_user}"


def _chunks(value: str, size: int) -> list[str]:
    return [value[index : index + size] for index in range(0, len(value), size)]


def _example_for_schema(schema: dict[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or properties.keys())
        return {
            name: _example_for_schema(child)
            for name, child in properties.items()
            if name in required
        }
    if kind == "array":
        return [_example_for_schema(schema.get("items") or {})]
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1.0))
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return str(schema.get("default", "example"))
