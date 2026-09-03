"""Anthropic Messages API adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from time import monotonic
from typing import Any

import httpx

from aegis_gateway.domain import (
    GatewayRequest,
    ModelRoute,
    ProviderResult,
    ProviderStreamEvent,
)
from aegis_gateway.errors import ProviderError
from aegis_gateway.providers.base import (
    ProviderAdapter,
    iter_sse_json,
    normalize_transport_error,
    raise_for_provider_status,
)


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.anthropic.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not configured",
                provider=self.name,
                retryable=False,
                code="provider_not_configured",
            )
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _payload(request: GatewayRequest, route: ModelRoute, *, stream: bool) -> dict[str, Any]:
        system_parts = [
            message.content
            for message in request.messages
            if message.role in {"system", "developer"}
        ]
        messages = [
            {"role": message.role, "content": message.content}
            for message in request.messages
            if message.role in {"user", "assistant"}
        ]
        payload: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
            "metadata": {"user_id": request.user_id or request.request_id},
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.response_schema is not None:
            payload["output_config"] = {
                "format": {"type": "json_schema", "schema": request.response_schema}
            }
        return payload

    async def complete(self, request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        started = monotonic()
        try:
            response = await self._client.post(
                f"{self._base_url}/messages",
                headers=self._headers(),
                json=self._payload(request, route, stream=False),
                timeout=request.max_latency_ms / 1000,
            )
            raise_for_provider_status(response, self.name)
            body = response.json()
        except Exception as exc:
            raise normalize_transport_error(exc, self.name) from exc
        elapsed = (monotonic() - started) * 1000
        usage = body.get("usage") or {}
        text = "".join(
            str(block.get("text", ""))
            for block in body.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return ProviderResult(
            provider_request_id=body.get("id"),
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            finish_reason=str(body.get("stop_reason", "end_turn")),
            ttft_ms=elapsed,
            latency_ms=elapsed,
            raw_model=str(body.get("model", route.model)),
        )

    async def stream(
        self, request: GatewayRequest, route: ModelRoute
    ) -> AsyncIterator[ProviderStreamEvent]:
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/messages",
                headers=self._headers(),
                json=self._payload(request, route, stream=True),
                timeout=request.max_latency_ms / 1000,
            ) as response:
                raise_for_provider_status(response, self.name)
                yield ProviderStreamEvent(type="start")
                input_tokens = 0
                async for event in iter_sse_json(response):
                    event_type = event.get("type")
                    if event_type == "message_start":
                        message = event.get("message") or {}
                        usage = message.get("usage") or {}
                        input_tokens = int(usage.get("input_tokens", 0))
                        yield ProviderStreamEvent(
                            type="start", provider_request_id=message.get("id")
                        )
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            yield ProviderStreamEvent(
                                type="delta", delta=str(delta.get("text", ""))
                            )
                    elif event_type == "message_delta":
                        usage = event.get("usage") or {}
                        yield ProviderStreamEvent(
                            type="usage",
                            input_tokens=input_tokens,
                            output_tokens=int(usage.get("output_tokens", 0)),
                        )
                    elif event_type == "message_stop":
                        yield ProviderStreamEvent(type="done", finish_reason="end_turn")
                    elif event_type == "error":
                        raise ProviderError(
                            "Anthropic stream reported a terminal error",
                            provider=self.name,
                            retryable=True,
                            code="provider_stream_error",
                        )
        except Exception as exc:
            raise normalize_transport_error(exc, self.name) from exc

    async def health(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()
