"""Provider protocol and transport parsing shared by all adapters."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

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


class ProviderAdapter(ABC):
    """Normalized contract implemented by every provider integration."""

    name: str

    @abstractmethod
    async def complete(self, request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        """Return one complete model response."""

    @abstractmethod
    async def stream(
        self, request: GatewayRequest, route: ModelRoute
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Yield provider-independent stream events."""
        if False:  # pragma: no cover - makes this an async generator contract
            yield ProviderStreamEvent(type="done")

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class ProviderRegistry:
    def __init__(self, adapters: list[ProviderAdapter]) -> None:
        self._adapters = {adapter.name: adapter for adapter in adapters}

    def get(self, provider: str) -> ProviderAdapter:
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise ProviderError(
                f"no adapter registered for provider '{provider}'",
                provider=provider,
                retryable=False,
                code="provider_not_configured",
            ) from exc

    async def health(self) -> dict[str, bool]:
        return {name: await adapter.health() for name, adapter in self._adapters.items()}

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()


def raise_for_provider_status(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    detail = _safe_error_detail(response)
    if response.status_code in {401, 403}:
        raise ProviderAuthError(detail, provider=provider)
    if response.status_code == 429:
        raise ProviderRateLimitError(detail, provider=provider)
    if response.status_code in {408, 504}:
        raise ProviderTimeoutError(detail, provider=provider)
    raise ProviderError(
        detail,
        provider=provider,
        status_code=502 if response.status_code >= 500 else 422,
        retryable=response.status_code >= 500,
        code="provider_unavailable" if response.status_code >= 500 else "invalid_provider_request",
    )


def normalize_transport_error(exc: Exception, provider: str) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError("upstream request timed out", provider=provider)
    if isinstance(exc, httpx.HTTPError):
        return ProviderError(
            "upstream transport failed",
            provider=provider,
            retryable=True,
            code="provider_transport_error",
        )
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(
        "unexpected provider adapter failure",
        provider=provider,
        retryable=False,
        code="provider_adapter_error",
    )


async def iter_sse_json(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse SSE frames without assuming transport chunks align with events."""

    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines.clear()
                if payload != "[DONE]":
                    value = json.loads(payload)
                    if isinstance(value, dict):
                        yield value
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        payload = "\n".join(data_lines)
        if payload != "[DONE]":
            value = json.loads(payload)
            if isinstance(value, dict):
                yield value


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"upstream returned HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:500]
        if isinstance(error, str):
            return error[:500]
    return f"upstream returned HTTP {response.status_code}"


def estimate_tokens(text: str) -> int:
    """Conservative provider-independent estimate used only before routing."""

    return max(1, (len(text) + 3) // 4)


def request_input_tokens(request: GatewayRequest) -> int:
    return sum(estimate_tokens(message.content) + 4 for message in request.messages)
