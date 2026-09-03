"""Ollama local chat adapter with NDJSON streaming."""

from __future__ import annotations

import json
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
    normalize_transport_error,
    raise_for_provider_status,
)


class OllamaAdapter(ProviderAdapter):
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient()

    @staticmethod
    def _payload(request: GatewayRequest, route: ModelRoute, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": route.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
                if message.role != "developer"
            ],
            "stream": stream,
            "options": {"num_predict": request.max_output_tokens},
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        if request.response_schema is not None:
            payload["format"] = request.response_schema
        return payload

    async def complete(self, request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        started = monotonic()
        try:
            response = await self._client.post(
                f"{self._base_url}/api/chat",
                json=self._payload(request, route, stream=False),
                timeout=request.max_latency_ms / 1000,
            )
            raise_for_provider_status(response, self.name)
            body = response.json()
        except Exception as exc:
            raise normalize_transport_error(exc, self.name) from exc
        elapsed = (monotonic() - started) * 1000
        message = body.get("message") or {}
        return ProviderResult(
            text=str(message.get("content", "")),
            input_tokens=int(body.get("prompt_eval_count", 0)),
            output_tokens=int(body.get("eval_count", 0)),
            finish_reason=str(body.get("done_reason", "stop")),
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
                f"{self._base_url}/api/chat",
                json=self._payload(request, route, stream=True),
                timeout=request.max_latency_ms / 1000,
            ) as response:
                raise_for_provider_status(response, self.name)
                yield ProviderStreamEvent(type="start")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    if "error" in event:
                        raise ProviderError(
                            str(event["error"]),
                            provider=self.name,
                            retryable=True,
                            code="provider_stream_error",
                        )
                    message = event.get("message") or {}
                    content = str(message.get("content", ""))
                    if content:
                        yield ProviderStreamEvent(type="delta", delta=content)
                    if event.get("done"):
                        yield ProviderStreamEvent(
                            type="usage",
                            input_tokens=int(event.get("prompt_eval_count", 0)),
                            output_tokens=int(event.get("eval_count", 0)),
                        )
                        yield ProviderStreamEvent(
                            type="done", finish_reason=str(event.get("done_reason", "stop"))
                        )
        except Exception as exc:
            raise normalize_transport_error(exc, self.name) from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get(f"{self._base_url}/api/tags", timeout=1)
            return response.is_success
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()
