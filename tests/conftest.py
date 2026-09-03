from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from aegis_gateway.config import Settings
from aegis_gateway.domain import (
    Capability,
    GatewayRequest,
    Message,
    ModelRoute,
    PrivacyMode,
)
from aegis_gateway.runtime import Runtime, create_runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def route_factory() -> Callable[..., ModelRoute]:
    def make(**overrides: Any) -> ModelRoute:
        values: dict[str, Any] = {
            "id": "route-a",
            "provider": "mock",
            "model": "model-a",
            "capabilities": {Capability.TEXT, Capability.STREAMING},
            "regions": {"local"},
            "context_window": 32_768,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "expected_p95_latency_ms": 100,
            "quality_score": 0.8,
            "privacy_modes": {
                PrivacyMode.STANDARD,
                PrivacyMode.ZERO_RETENTION,
                PrivacyMode.LOCAL_ONLY,
            },
            "enabled": True,
        }
        values.update(overrides)
        return ModelRoute.model_validate(values)

    return make


@pytest.fixture
def request_factory() -> Callable[..., GatewayRequest]:
    def make(**overrides: Any) -> GatewayRequest:
        values: dict[str, Any] = {
            "tenant_id": "tenant-a",
            "request_id": "request-0001",
            "messages": [Message(role="user", content="hello gateway")],
            "max_cost_usd": 1.0,
            "max_latency_ms": 10_000,
        }
        values.update(overrides)
        return GatewayRequest.model_validate(values)

    return make


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = Settings(
        database_path=tmp_path / "aegis.db",
        model_catalog_path=ROOT / "configs/models.yaml",
        admin_token="test-admin-token",
        request_rate_per_second=10_000,
        request_burst=10_000,
        log_level="WARNING",
    )
    value = create_runtime(settings)
    await value.initialize()
    yield value
    await value.aclose()
