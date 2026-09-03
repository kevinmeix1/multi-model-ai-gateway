"""OpenAI Responses API adapter using the current REST and SSE contracts."""

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


class OpenAIAdapter(ProviderAdapter):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "OPENAI_API_KEY is not configured",
                provider=self.name,
                retryable=False,
                code="provider_not_configured",
            )
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _payload(request: GatewayRequest, route: ModelRoute, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": route.model,
            "input": [
                {"role": message.role, "content": message.content}
                for message in request.messages
                if message.role != "tool"
            ],
            "max_output_tokens": request.max_output_tokens,
            "stream": stream,
            "store": False,
            "metadata": {"gateway_request_id": request.request_id, "tenant": request.tenant_id},
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.response_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.response_schema,
                }
            }
        return payload

    async def complete(self, request: GatewayRequest, route: ModelRoute) -> ProviderResult:
        started = monotonic()
        try:
            response = await self._client.post(
                f"{self._base_url}/responses",
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
        return ProviderResult(
            provider_request_id=body.get("id"),
            text=_extract_output_text(body),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            finish_reason=str(body.get("status", "completed")),
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
                f"{self._base_url}/responses",
                headers=self._headers(),
                json=self._payload(request, route, stream=True),
                timeout=request.max_latency_ms / 1000,
            ) as response:
                raise_for_provider_status(response, self.name)
                yield ProviderStreamEvent(type="start")
                async for event in iter_sse_json(response):
                    event_type = event.get("type")
                    if event_type == "response.created":
                        envelope = event.get("response") or {}
                        yield ProviderStreamEvent(
                            type="start", provider_request_id=envelope.get("id")
                        )
                    elif event_type == "response.output_text.delta":
                        yield ProviderStreamEvent(type="delta", delta=str(event.get("delta", "")))
                    elif event_type == "response.completed":
                        envelope = event.get("response") or {}
                        usage = envelope.get("usage") or {}
                        yield ProviderStreamEvent(
                            type="usage",
                            input_tokens=int(usage.get("input_tokens", 0)),
                            output_tokens=int(usage.get("output_tokens", 0)),
                        )
                        yield ProviderStreamEvent(type="done", finish_reason="completed")
                    elif event_type in {"response.failed", "error"}:
                        raise ProviderError(
                            "OpenAI stream reported a terminal error",
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


def _extract_output_text(body: dict[str, Any]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str):
        return direct
    fragments: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                fragments.append(str(content.get("text", "")))
    return "".join(fragments)
