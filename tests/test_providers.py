from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from aegis_gateway.domain import GatewayRequest, ModelRoute
from aegis_gateway.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from aegis_gateway.providers.anthropic import AnthropicAdapter
from aegis_gateway.providers.base import (
    ProviderRegistry,
    estimate_tokens,
    iter_sse_json,
    normalize_transport_error,
    raise_for_provider_status,
    request_input_tokens,
)
from aegis_gateway.providers.mock import MockAdapter
from aegis_gateway.providers.ollama import OllamaAdapter
from aegis_gateway.providers.openai import OpenAIAdapter


def json_response(request: httpx.Request, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, request=request, json=body)


async def test_openai_complete_builds_current_responses_payload(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return json_response(
            request,
            {
                "id": "resp-1",
                "model": "model-returned",
                "status": "completed",
                "output_text": '{"status":"ok"}',
                "usage": {"input_tokens": 7, "output_tokens": 4},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAIAdapter(api_key="test-key", base_url="https://openai.test/v1/", client=client)
    request = request_factory(
        temperature=0.2,
        response_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
        schema_name="health",
        messages=[
            {"role": "developer", "content": "be concise"},
            {"role": "user", "content": "health"},
            {"role": "tool", "content": "excluded"},
        ],
    )
    result = await adapter.complete(request, route_factory(provider="openai"))
    assert result.text == '{"status":"ok"}'
    assert result.input_tokens == 7
    assert result.raw_model == "model-returned"
    assert captured["store"] is False
    assert captured["temperature"] == 0.2
    assert captured["text"] == {
        "format": {
            "type": "json_schema",
            "name": "health",
            "strict": True,
            "schema": request.response_schema,
        }
    }
    assert len(captured["input"]) == 2  # type: ignore[arg-type]
    await client.aclose()


async def test_openai_extracts_nested_output_and_streams_sse(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    bodies = [
        {
            "id": "resp-2",
            "output": [
                {"type": "ignored"},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "hello "},
                        {"type": "refusal", "text": "ignored"},
                        {"type": "output_text", "text": "world"},
                    ],
                },
            ],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["stream"]:
            stream = "".join(
                [
                    'data: {"type":"response.created","response":{"id":"resp-s"}}\n\n',
                    'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
                    'data: {"type":"response.completed","response":{"usage":',
                    '{"input_tokens":2,"output_tokens":1}}}\n\n',
                    "data: [DONE]\n\n",
                ]
            )
            return httpx.Response(200, request=request, text=stream)
        return json_response(request, bodies[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAIAdapter(api_key="key", client=client)
    route = route_factory(provider="openai")
    request = request_factory()
    assert (await adapter.complete(request, route)).text == "hello world"
    events = [event async for event in adapter.stream(request, route)]
    assert [event.type for event in events] == ["start", "start", "delta", "usage", "done"]
    assert events[2].delta == "hi"
    assert events[3].output_tokens == 1
    assert await adapter.health() is True
    await adapter.aclose()
    await client.aclose()


async def test_openai_configuration_and_stream_errors_are_normalized(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    missing = OpenAIAdapter(api_key=None)
    with pytest.raises(ProviderError, match="not configured") as captured:
        await missing.complete(request_factory(), route_factory(provider="openai"))
    assert captured.value.code == "provider_not_configured"
    assert await missing.health() is False
    await missing.aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text='data: {"type":"response.failed"}\n\n',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAIAdapter(api_key="key", client=client)
    with pytest.raises(ProviderError) as stream_error:
        _ = [
            event
            async for event in adapter.stream(
                request_factory(stream=True), route_factory(provider="openai")
            )
        ]
    assert stream_error.value.code == "provider_stream_error"
    await client.aclose()


async def test_anthropic_complete_and_stream_contracts(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        if payload["stream"]:
            return httpx.Response(
                200,
                request=request,
                text="".join(
                    [
                        'data: {"type":"message_start","message":{"id":"msg-s",',
                        '"usage":{"input_tokens":3}}}\n\n',
                        'data: {"type":"content_block_delta","delta":',
                        '{"type":"text_delta","text":"hello"}}\n\n',
                        'data: {"type":"content_block_delta","delta":{"type":"citation"}}\n\n',
                        'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n',
                        'data: {"type":"message_stop"}\n\n',
                    ]
                ),
            )
        return json_response(
            request,
            {
                "id": "msg-1",
                "model": "claude-returned",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "tool_use"},
                    {"type": "text", "text": "world"},
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter(api_key="key", base_url="https://anthropic.test/v1/", client=client)
    request = request_factory(
        user_id="user-1",
        temperature=0.1,
        response_schema={"type": "string"},
        messages=[
            {"role": "system", "content": "system"},
            {"role": "developer", "content": "developer"},
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": "excluded"},
        ],
    )
    route = route_factory(provider="anthropic")
    result = await adapter.complete(request, route)
    assert result.text == "hello world"
    events = [event async for event in adapter.stream(request, route)]
    assert [event.type for event in events] == ["start", "start", "delta", "usage", "done"]
    assert captured[0]["system"] == "system\n\ndeveloper"
    assert captured[0]["metadata"] == {"user_id": "user-1"}
    assert captured[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "string"}}
    }
    assert captured[0]["temperature"] == 0.1
    assert await adapter.health() is True
    await client.aclose()


async def test_anthropic_missing_key_and_stream_error(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    missing = AnthropicAdapter(api_key=None)
    assert await missing.health() is False
    with pytest.raises(ProviderError, match="not configured"):
        await missing.complete(request_factory(), route_factory(provider="anthropic"))
    await missing.aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text='data: {"type":"error"}\n\n')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicAdapter(api_key="key", client=client)
    with pytest.raises(ProviderError, match="terminal error"):
        _ = [
            event
            async for event in adapter.stream(
                request_factory(stream=True), route_factory(provider="anthropic")
            )
        ]
    await adapter.aclose()
    await client.aclose()


async def test_ollama_complete_stream_payload_and_health(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, request=request, json={"models": []})
        payload = json.loads(request.content)
        captured.append(payload)
        if payload["stream"]:
            return httpx.Response(
                200,
                request=request,
                text="\n".join(
                    [
                        "",
                        '{"message":{"content":"local"},"done":false}',
                        "[]",
                        '{"message":{"content":""},"done":true,'
                        '"prompt_eval_count":4,"eval_count":2,"done_reason":"stop"}',
                    ]
                ),
            )
        return json_response(
            request,
            {
                "model": "qwen-returned",
                "message": {"content": "local answer"},
                "prompt_eval_count": 4,
                "eval_count": 2,
                "done_reason": "stop",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaAdapter(base_url="http://ollama.test/", client=client)
    request = request_factory(
        temperature=0.4,
        response_schema={"type": "object"},
        messages=[
            {"role": "developer", "content": "excluded"},
            {"role": "user", "content": "hello"},
        ],
    )
    route = route_factory(provider="ollama")
    result = await adapter.complete(request, route)
    assert result.text == "local answer"
    events = [event async for event in adapter.stream(request, route)]
    assert [event.type for event in events] == ["start", "delta", "usage", "done"]
    assert captured[0]["options"] == {"num_predict": 512, "temperature": 0.4}
    assert captured[0]["format"] == {"type": "object"}
    assert len(captured[0]["messages"]) == 1  # type: ignore[arg-type]
    assert await adapter.health() is True
    await client.aclose()


async def test_ollama_stream_error_and_failed_health(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, request=request, text='{"error":"model missing"}\n')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaAdapter(client=client)
    assert await adapter.health() is False
    with pytest.raises(ProviderError, match="model missing"):
        _ = [
            event
            async for event in adapter.stream(
                request_factory(stream=True), route_factory(provider="ollama")
            )
        ]
    await adapter.aclose()
    await client.aclose()


def test_provider_status_and_transport_error_taxonomy() -> None:
    request = httpx.Request("POST", "https://provider.test")
    raise_for_provider_status(httpx.Response(200, request=request), "provider")
    cases = [
        (401, {"error": {"message": "bad key"}}, ProviderAuthError),
        (429, {"error": "slow down"}, ProviderRateLimitError),
        (504, {}, ProviderTimeoutError),
        (500, {}, ProviderError),
        (400, {}, ProviderError),
    ]
    for status, body, error_type in cases:
        with pytest.raises(error_type):
            raise_for_provider_status(
                httpx.Response(status, request=request, json=body), "provider"
            )
    with pytest.raises(ProviderError, match="HTTP 502"):
        raise_for_provider_status(httpx.Response(502, request=request, text="not json"), "provider")
    assert isinstance(
        normalize_transport_error(httpx.ReadTimeout("timeout"), "p"), ProviderTimeoutError
    )
    assert (
        normalize_transport_error(httpx.ConnectError("failed"), "p").code
        == "provider_transport_error"
    )
    original = ProviderError("original", provider="p")
    assert normalize_transport_error(original, "p") is original
    assert normalize_transport_error(RuntimeError("bug"), "p").code == "provider_adapter_error"


async def test_sse_parser_registry_mock_faults_and_token_estimates(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    request = httpx.Request("GET", "https://stream.test")
    response = httpx.Response(
        200,
        request=request,
        text='data: {"value":\ndata: 1}\n\ndata: [DONE]\n\ndata: {"tail":true}',
    )
    assert [value async for value in iter_sse_json(response)] == [{"value": 1}, {"tail": True}]

    mock = MockAdapter(latency_ms=0)
    registry = ProviderRegistry([mock])
    assert registry.get("mock") is mock
    assert await registry.health() == {"mock": True}
    with pytest.raises(ProviderError) as missing:
        registry.get("missing")
    assert missing.value.code == "provider_not_configured"
    route = route_factory()
    generated = await mock.complete(request_factory(), route)
    assert generated.text.startswith("Deterministic response")
    structured = await mock.complete(
        request_factory(
            response_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 2},
                    "enabled": {"type": "boolean"},
                    "items": {"type": "array", "items": {"const": "x"}},
                    "nullable": {"type": "null"},
                },
                "required": ["count", "enabled", "items", "nullable"],
            }
        ),
        route,
    )
    assert json.loads(structured.text) == {
        "count": 2,
        "enabled": True,
        "items": ["x"],
        "nullable": None,
    }
    events = [event async for event in mock.stream(request_factory(stream=True), route)]
    assert events[0].type == "start"
    assert events[-1].type == "done"
    for fault, error_type in (
        ("rate_limit", ProviderRateLimitError),
        ("timeout", ProviderTimeoutError),
        ("auth", ProviderAuthError),
        ("unavailable", ProviderError),
    ):
        with pytest.raises(error_type):
            await mock.complete(request_factory(metadata={"mock_failure": fault}), route)
    assert estimate_tokens("") == 1
    assert request_input_tokens(request_factory()) >= 5
    await registry.aclose()
