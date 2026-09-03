from __future__ import annotations

import pytest

from aegis_gateway.control.evaluation import EvaluationRunner
from aegis_gateway.domain import (
    ArtifactKind,
    EvaluationCaseResult,
    EvaluationRun,
    Release,
)
from aegis_gateway.errors import EvaluationGateError
from aegis_gateway.runtime import Runtime


async def test_offline_evaluation_scores_text_schema_and_persists(runtime: Runtime) -> None:
    await runtime.artifacts.put(
        kind=ArtifactKind.DATASET,
        name="smoke",
        version="1",
        content={
            "cases": [
                {
                    "id": "text",
                    "messages": [{"role": "user", "content": "explain routing"}],
                    "expected_contains": ["EXPLAIN   ROUTING", "missing fragment"],
                },
                {
                    "id": "schema",
                    "messages": [{"role": "user", "content": "health"}],
                    "response_schema": {
                        "type": "object",
                        "properties": {"status": {"const": "ok"}},
                        "required": ["status"],
                    },
                },
                {
                    "id": "nonempty",
                    "messages": [{"role": "user", "content": "anything"}],
                },
            ]
        },
    )
    run = await runtime.evaluations.run(
        dataset_name="smoke", dataset_version=None, route_id="mock-primary"
    )
    assert len(run.results) == 3
    assert run.results[0].quality_score == 0.5
    assert run.results[0].passed is False
    assert run.results[1].schema_valid is True
    assert run.results[1].passed is True
    assert run.results[2].passed is True
    assert run.schema_compliance == 1
    assert run.p99_latency_ms >= 0
    assert run.total_cost_usd == 0
    assert (await runtime.evidence.list_evaluations())[0].id == run.id
    assert await runtime.evidence.metrics() == []


async def test_evaluation_converts_case_exceptions_to_results(runtime: Runtime) -> None:
    await runtime.artifacts.put(
        kind=ArtifactKind.DATASET,
        name="failure",
        version="1",
        content={
            "cases": [
                {
                    "id": "case",
                    "messages": [{"role": "user", "content": "hello"}],
                    "response_schema": {"type": "string"},
                }
            ]
        },
    )
    run = await runtime.evaluations.run(
        dataset_name="failure", dataset_version="1", route_id="unknown-route"
    )
    assert run.pass_rate == 0
    assert run.schema_compliance == 0
    assert run.results[0].error == "NoEligibleRouteError"


async def test_evaluation_rejects_empty_dataset(runtime: Runtime) -> None:
    await runtime.artifacts.put(
        kind=ArtifactKind.DATASET,
        name="empty",
        version="1",
        content={"cases": []},
    )
    with pytest.raises(ValueError, match="non-empty"):
        await runtime.evaluations.run(
            dataset_name="empty", dataset_version="1", route_id="mock-primary"
        )


def evaluation_run(**overrides: object) -> EvaluationRun:
    values: dict[str, object] = {
        "id": "eval-1",
        "dataset_name": "dataset",
        "dataset_version": "1",
        "route_id": "candidate",
        "results": [
            EvaluationCaseResult(
                case_id="case-1",
                route_id="candidate",
                passed=True,
                quality_score=1,
                schema_valid=True,
                latency_ms=10,
                cost_usd=0.01,
            )
        ],
        "pass_rate": 1,
        "mean_quality": 1,
        "schema_compliance": 1,
        "p99_latency_ms": 10,
        "total_cost_usd": 0.01,
    }
    values.update(overrides)
    return EvaluationRun.model_validate(values)


def test_release_gate_passes_or_reports_all_quantitative_breaches() -> None:
    release = Release(
        id="release-1",
        name="default",
        baseline_route_id="baseline",
        candidate_route_id="candidate",
        min_quality_score=0.9,
        min_schema_compliance=0.95,
        max_p99_latency_ms=100,
        max_error_rate=0.1,
    )
    EvaluationRunner.enforce_release_gate(release, evaluation_run())
    failed = evaluation_run(
        mean_quality=0.5,
        schema_compliance=0.5,
        p99_latency_ms=200,
        pass_rate=0.5,
    )
    with pytest.raises(EvaluationGateError) as captured:
        EvaluationRunner.enforce_release_gate(release, failed)
    message = str(captured.value)
    assert "quality=" in message
    assert "schema=" in message
    assert "p99=" in message
    assert "failure_rate=" in message
