from __future__ import annotations

from collections.abc import Callable

import aiosqlite
import pytest

from aegis_gateway.control.registry import Database, summarize_metrics
from aegis_gateway.domain import (
    ArtifactKind,
    EvaluationCaseResult,
    EvaluationRun,
    GatewayRequest,
    MetricsSnapshot,
    Release,
    ReleaseState,
    RequestMetric,
)
from aegis_gateway.errors import ArtifactNotFoundError, RegistryConflictError
from aegis_gateway.runtime import Runtime


def release(release_id: str = "release-1", **overrides: object) -> Release:
    values: dict[str, object] = {
        "id": release_id,
        "name": "default",
        "baseline_route_id": "mock-primary",
        "candidate_route_id": "mock-canary",
        "canary_percent": 100,
        "shadow_percent": 0,
        "min_canary_samples": 2,
        "max_error_rate": 0.1,
        "max_p99_latency_ms": 100,
        "min_schema_compliance": 0.9,
        "min_quality_score": 0.8,
    }
    values.update(overrides)
    return Release.model_validate(values)


def metric(index: int, **overrides: object) -> RequestMetric:
    values: dict[str, object] = {
        "request_id": f"request-{index:04d}",
        "tenant_id": "tenant-a",
        "route_id": "mock-canary",
        "provider": "mock",
        "success": True,
        "schema_valid": True,
        "cache_hit": False,
        "canary": True,
        "release_id": "release-1",
        "shadow": False,
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.01,
        "ttft_ms": 10,
        "latency_ms": float(20 + index),
        "routing_regret": 0.02,
        "fallback_count": 0,
    }
    values.update(overrides)
    return RequestMetric.model_validate(values)


async def test_database_initialization_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "nested" / "registry.db")
    await database.initialize()
    await database.initialize()
    assert database.path.exists()


async def test_artifacts_are_immutable_versioned_and_queryable(runtime: Runtime) -> None:
    first = await runtime.artifacts.put(
        kind=ArtifactKind.PROMPT,
        name="assistant",
        version="1",
        content={"template": "hello"},
    )
    same = await runtime.artifacts.put(
        kind=ArtifactKind.PROMPT,
        name="assistant",
        version="1",
        content={"template": "hello"},
    )
    assert same.content_hash == first.content_hash
    with pytest.raises(RegistryConflictError):
        await runtime.artifacts.put(
            kind=ArtifactKind.PROMPT,
            name="assistant",
            version="1",
            content={"template": "changed"},
        )
    second = await runtime.artifacts.put(
        kind=ArtifactKind.PROMPT,
        name="assistant",
        version="2",
        content={"template": "new"},
    )
    latest = await runtime.artifacts.get(kind=ArtifactKind.PROMPT, name="assistant")
    assert latest.version == second.version
    assert len(await runtime.artifacts.list()) == 2
    assert len(await runtime.artifacts.list(kind=ArtifactKind.PROMPT)) == 2
    with pytest.raises(ArtifactNotFoundError):
        await runtime.artifacts.get(kind=ArtifactKind.DATASET, name="missing")


async def test_release_registry_state_machine_and_live_priority(runtime: Runtime) -> None:
    first = await runtime.release_registry.create(release("release-1"))
    assert (await runtime.release_registry.get(first.id)).state is ReleaseState.DRAFT
    with pytest.raises(RegistryConflictError):
        await runtime.release_registry.create(first)
    with pytest.raises(ArtifactNotFoundError):
        await runtime.release_registry.get("missing")
    with pytest.raises(ArtifactNotFoundError):
        await runtime.release_registry.set_state("missing", ReleaseState.ACTIVE)

    active = await runtime.release_registry.set_state(first.id, ReleaseState.ACTIVE)
    assert active.state is ReleaseState.ACTIVE
    second = await runtime.release_registry.create(release("release-2"))
    candidate = await runtime.release_registry.set_state(second.id, ReleaseState.CANDIDATE)
    assert (await runtime.release_registry.get_live()).id == candidate.id  # type: ignore[union-attr]
    await runtime.release_registry.set_state(second.id, ReleaseState.ACTIVE)
    assert (await runtime.release_registry.get(first.id)).state is ReleaseState.ROLLED_BACK
    assert len(await runtime.release_registry.list()) == 2
    assert await runtime.release_registry.get_live("unknown") is None


async def test_evidence_summary_filters_and_evaluation_storage(runtime: Runtime) -> None:
    await runtime.evidence.record_metric(metric(1, cache_hit=True, fallback_count=1))
    await runtime.evidence.record_metric(
        metric(
            2,
            success=False,
            schema_valid=False,
            cost_usd=0,
            fallback_count=1,
            error_code="provider_timeout",
        )
    )
    await runtime.evidence.record_metric(
        metric(3, route_id="mock-primary", canary=False, release_id=None, schema_valid=None)
    )
    summary = await runtime.evidence.summary(release_id="release-1", canary=True)
    assert summary.requests == 2
    assert summary.success_rate == 0.5
    assert summary.schema_compliance == 0.5
    assert summary.cache_hit_rate == 0.5
    assert summary.failover_success_rate == 0.5
    assert summary.mean_cost_per_success_usd == 0.01
    assert summary.p99_latency_ms == 22
    assert len(await runtime.evidence.metrics(route_id="mock-primary", limit=1)) == 1

    run = EvaluationRun(
        id="eval-1",
        dataset_name="dataset",
        dataset_version="1",
        route_id="mock-primary",
        results=[
            EvaluationCaseResult(
                case_id="case-1",
                route_id="mock-primary",
                passed=True,
                quality_score=1,
                schema_valid=None,
                latency_ms=10,
                cost_usd=0,
            )
        ],
        pass_rate=1,
        mean_quality=1,
        schema_compliance=None,
        p99_latency_ms=10,
        total_cost_usd=0,
    )
    await runtime.evidence.put_evaluation(run)
    assert (await runtime.evidence.list_evaluations(limit=1))[0].id == "eval-1"


def test_empty_and_failed_metric_summaries_are_well_defined() -> None:
    empty = summarize_metrics([])
    assert empty == MetricsSnapshot(
        requests=0,
        successful_requests=0,
        success_rate=0,
        schema_compliance=None,
        cache_hit_rate=0,
        failover_success_rate=None,
        mean_cost_per_success_usd=None,
        mean_ttft_ms=None,
        p99_latency_ms=None,
        mean_routing_regret=None,
    )
    failed = summarize_metrics([metric(1, success=False, schema_valid=None)])
    assert failed.mean_cost_per_success_usd is None


async def test_release_assignment_is_stable_and_policy_aware(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    request = request_factory(request_id="stable-request")
    assert (await runtime.release_manager.assignment(request)).release_id is None
    created = await runtime.release_registry.create(
        release("release-1", canary_percent=100, shadow_percent=100)
    )
    await runtime.release_manager.start_canary(created.id)
    first = await runtime.release_manager.assignment(request)
    second = await runtime.release_manager.assignment(request)
    assert first == second
    assert first.canary is True
    assert first.preferred_route_id == "mock-canary"

    await runtime.release_manager.promote(created.id)
    active = await runtime.release_manager.assignment(request)
    assert active.preferred_route_id == "mock-canary"
    assert active.canary is False


async def test_shadow_assignment_respects_request_opt_out(
    runtime: Runtime,
    request_factory: Callable[..., GatewayRequest],
) -> None:
    created = await runtime.release_registry.create(
        release("release-1", canary_percent=0, shadow_percent=100)
    )
    await runtime.release_manager.start_canary(created.id)
    shadow = await runtime.release_manager.assignment(request_factory())
    assert shadow.preferred_route_id == "mock-primary"
    assert shadow.shadow_route_id == "mock-canary"
    opted_out = await runtime.release_manager.assignment(request_factory(shadow_enabled=False))
    assert opted_out.shadow_route_id is None


async def test_release_assessment_rolls_back_on_live_slo_breach(runtime: Runtime) -> None:
    created = await runtime.release_registry.create(release())
    assert (await runtime.release_manager.assess(created.id)).rolled_back is False
    await runtime.release_manager.start_canary(created.id)
    assert (await runtime.release_manager.assess(created.id)).rolled_back is False
    await runtime.evidence.record_metric(metric(1, success=False, schema_valid=False))
    await runtime.evidence.record_metric(metric(2, latency_ms=200))
    decision = await runtime.release_manager.assess(created.id)
    assert decision.rolled_back is True
    assert "error_rate" in (decision.reason or "")
    assert "p99_ms" in (decision.reason or "")
    assert "schema_compliance" in (decision.reason or "")
    assert decision.duration_ms is not None
    assert (await runtime.release_registry.get(created.id)).state is ReleaseState.ROLLED_BACK

    async with aiosqlite.connect(runtime.database.path) as database:
        count = (await (await database.execute("SELECT COUNT(*) FROM rollback_events")).fetchone())[
            0
        ]
    assert count == 1


async def test_manual_rollback_records_reason(runtime: Runtime) -> None:
    created = await runtime.release_registry.create(release())
    decision = await runtime.release_manager.rollback(created.id, "operator")
    assert decision.reason == "operator"
    assert decision.rolled_back is True
