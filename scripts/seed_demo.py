"""Seed immutable local demo artifacts and a draft canary release."""

from __future__ import annotations

import asyncio

from aegis_gateway.domain import ArtifactKind, Release
from aegis_gateway.errors import RegistryConflictError
from aegis_gateway.runtime import create_runtime


async def seed() -> None:
    runtime = create_runtime()
    await runtime.initialize()
    await runtime.artifacts.put(
        kind=ArtifactKind.PROMPT,
        name="concise-assistant",
        version="1.0.0",
        content={
            "template": "Answer for audience {{ audience }} in at most {{ sentences }} sentences.",
            "variables": ["audience", "sentences"],
        },
    )
    await runtime.artifacts.put(
        kind=ArtifactKind.DATASET,
        name="gateway-smoke",
        version="1.0.0",
        content={
            "cases": [
                {
                    "id": "text-001",
                    "messages": [{"role": "user", "content": "Explain a circuit breaker."}],
                    "expected_contains": ["Explain a circuit breaker."],
                    "tags": ["text", "resilience"],
                },
                {
                    "id": "schema-001",
                    "messages": [{"role": "user", "content": "Return service health."}],
                    "response_schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                        "additionalProperties": False,
                    },
                    "tags": ["structured-output"],
                },
            ]
        },
    )
    try:
        await runtime.release_registry.create(
            Release(
                id="demo-release-v1",
                name="default",
                baseline_route_id="mock-primary",
                candidate_route_id="mock-canary",
                canary_percent=10,
                shadow_percent=20,
                min_canary_samples=10,
                max_error_rate=0.05,
                max_p99_latency_ms=500,
                min_schema_compliance=1.0,
                min_quality_score=1.0,
            )
        )
    except RegistryConflictError:
        pass
    await runtime.aclose()
    print("Seeded prompt, evaluation dataset, and demo release.")


if __name__ == "__main__":
    asyncio.run(seed())
