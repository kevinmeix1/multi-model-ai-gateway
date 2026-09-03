from __future__ import annotations

from datetime import UTC, datetime

from aegis_gateway.control.cache import HashingEmbedder, SemanticCache
from aegis_gateway.domain import (
    DataClassification,
    GatewayResponse,
    GatewayUsage,
    PrivacyMode,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def response(request_id: str = "request-0001") -> GatewayResponse:
    return GatewayResponse(
        request_id=request_id,
        route_id="route-a",
        provider="mock",
        model="model-a",
        text="cached answer",
        usage=GatewayUsage(input_tokens=3, output_tokens=2, cost_usd=0),
        ttft_ms=1,
        latency_ms=2,
        created_at=datetime.now(UTC),
    )


async def test_exact_and_semantic_hits_are_tenant_scoped(request_factory) -> None:  # type: ignore[no-untyped-def]
    cache = SemanticCache(ttl_seconds=10, similarity_threshold=0.5)
    original = request_factory(messages=[{"role": "user", "content": "red green blue"}])
    await cache.put(original, response())
    assert (await cache.get(original)).cache_hit is True  # type: ignore[union-attr]
    similar = request_factory(
        request_id="request-0002",
        messages=[{"role": "user", "content": "red green"}],
    )
    assert await cache.get(similar) is not None
    other_tenant = similar.model_copy(update={"tenant_id": "tenant-b"})
    assert await cache.get(other_tenant) is None


async def test_cache_scope_includes_privacy_schema_and_classification(request_factory) -> None:  # type: ignore[no-untyped-def]
    cache = SemanticCache(ttl_seconds=10, similarity_threshold=0.1)
    request = request_factory()
    await cache.put(request, response())
    assert (
        await cache.get(request.model_copy(update={"privacy_mode": PrivacyMode.LOCAL_ONLY})) is None
    )
    assert (
        await cache.get(
            request.model_copy(update={"data_classification": DataClassification.RESTRICTED})
        )
        is None
    )
    assert (
        await cache.get(request.model_copy(update={"response_schema": {"type": "string"}})) is None
    )
    assert await cache.get(request.model_copy(update={"allowed_regions": {"eu-west"}})) is None
    assert await cache.get(request.model_copy(update={"max_latency_ms": 500})) is None
    assert await cache.get(request.model_copy(update={"max_cost_usd": 0.1})) is None
    assert await cache.get(request.model_copy(update={"temperature": 0.5})) is None


async def test_expiration_lru_and_clear(request_factory) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    cache = SemanticCache(
        ttl_seconds=5,
        similarity_threshold=1,
        max_entries=1,
        clock=clock,
    )
    first = request_factory(
        request_id="request-0001", messages=[{"role": "user", "content": "one"}]
    )
    second = request_factory(
        request_id="request-0002", messages=[{"role": "user", "content": "two"}]
    )
    await cache.put(first, response())
    await cache.put(second, response("request-0002"))
    assert await cache.get(first) is None
    assert await cache.size() == 1
    clock.now = 5
    assert await cache.get(second) is None
    assert await cache.clear() == 0


def test_hashing_embedder_validation_and_empty_vector() -> None:
    assert HashingEmbedder().embed("") == {}
    try:
        HashingEmbedder(0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected validation error")
