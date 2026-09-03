from pathlib import Path

import pytest
from pydantic import ValidationError

from aegis_gateway.config import load_model_catalog
from aegis_gateway.domain import Capability, GatewayRequest, Message


def test_request_infers_stream_and_schema_capabilities() -> None:
    request = GatewayRequest(
        tenant_id="acme",
        messages=[Message(role="user", content="status")],
        stream=True,
        response_schema={"type": "object"},
    )
    assert request.required_capabilities == {
        Capability.TEXT,
        Capability.STREAMING,
        Capability.STRUCTURED_OUTPUT,
    }


def test_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate(
            {
                "tenant_id": "acme",
                "messages": [{"role": "user", "content": "hello"}],
                "surprise": True,
            }
        )


def test_catalog_loads_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    valid = tmp_path / "valid.yaml"
    valid.write_text(
        """routes:
  - id: local
    provider: mock
    model: deterministic
    capabilities: [text]
    regions: [local]
    context_window: 100
    input_cost_per_million: 0
    output_cost_per_million: 0
    expected_p95_latency_ms: 10
    quality_score: 0.5
    privacy_modes: [local_only]
""",
        encoding="utf-8",
    )
    assert load_model_catalog(valid)[0].id == "local"
    duplicate = tmp_path / "duplicate.yaml"
    route_yaml = valid.read_text(encoding="utf-8").split("routes:\n", 1)[1]
    duplicate.write_text(f"routes:\n{route_yaml}{route_yaml}", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_model_catalog(duplicate)


def test_catalog_requires_routes_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("models: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="routes"):
        load_model_catalog(path)
