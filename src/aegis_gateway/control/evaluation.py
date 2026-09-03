"""Deterministic offline evaluation and release-gate logic."""

from __future__ import annotations

import math
import re
from typing import Protocol
from uuid import uuid4

from aegis_gateway.control.registry import ArtifactRegistry, EvidenceStore
from aegis_gateway.domain import (
    ArtifactKind,
    CacheMode,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationRun,
    GatewayRequest,
    GatewayResponse,
    Release,
)
from aegis_gateway.errors import EvaluationGateError


class GenerationService(Protocol):
    async def generate(
        self,
        request: GatewayRequest,
        *,
        forced_route_id: str | None = None,
        evaluation: bool = False,
        shadow: bool = False,
    ) -> GatewayResponse: ...


class EvaluationRunner:
    def __init__(
        self,
        *,
        artifacts: ArtifactRegistry,
        evidence: EvidenceStore,
        service: GenerationService,
    ) -> None:
        self._artifacts = artifacts
        self._evidence = evidence
        self._service = service

    async def run(
        self,
        *,
        dataset_name: str,
        dataset_version: str | None,
        route_id: str,
    ) -> EvaluationRun:
        artifact = await self._artifacts.get(
            kind=ArtifactKind.DATASET, name=dataset_name, version=dataset_version
        )
        raw_cases = artifact.content.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("evaluation dataset must contain a non-empty cases list")
        cases = [EvaluationCase.model_validate(item) for item in raw_cases]
        results: list[EvaluationCaseResult] = []
        for case in cases:
            request = GatewayRequest(
                tenant_id="offline-evaluation",
                request_id=f"eval-{uuid4()}",
                messages=case.messages,
                response_schema=case.response_schema,
                max_cost_usd=100,
                max_latency_ms=120_000,
                cache_mode=CacheMode.BYPASS,
                shadow_enabled=False,
                metadata={"evaluation_case_id": case.id},
            )
            try:
                response = await self._service.generate(
                    request, forced_route_id=route_id, evaluation=True
                )
                quality = _quality(case, response)
                schema_ok = response.schema_valid is not False
                passed = quality >= 1.0 and schema_ok
                results.append(
                    EvaluationCaseResult(
                        case_id=case.id,
                        route_id=route_id,
                        passed=passed,
                        quality_score=quality,
                        schema_valid=response.schema_valid,
                        latency_ms=response.latency_ms,
                        cost_usd=response.usage.cost_usd,
                    )
                )
            except Exception as exc:
                results.append(
                    EvaluationCaseResult(
                        case_id=case.id,
                        route_id=route_id,
                        passed=False,
                        quality_score=0,
                        schema_valid=False if case.response_schema else None,
                        latency_ms=0,
                        cost_usd=0,
                        error=type(exc).__name__,
                    )
                )
        schemas = [item.schema_valid for item in results if item.schema_valid is not None]
        run = EvaluationRun(
            id=str(uuid4()),
            dataset_name=artifact.name,
            dataset_version=artifact.version,
            route_id=route_id,
            results=results,
            pass_rate=sum(item.passed for item in results) / len(results),
            mean_quality=sum(item.quality_score for item in results) / len(results),
            schema_compliance=(sum(bool(item) for item in schemas) / len(schemas))
            if schemas
            else None,
            p99_latency_ms=_percentile([item.latency_ms for item in results], 0.99),
            total_cost_usd=sum(item.cost_usd for item in results),
        )
        await self._evidence.put_evaluation(run)
        return run

    @staticmethod
    def enforce_release_gate(release: Release, candidate: EvaluationRun) -> None:
        failures: list[str] = []
        if candidate.mean_quality < release.min_quality_score:
            failures.append(f"quality={candidate.mean_quality:.4f}<{release.min_quality_score:.4f}")
        if (
            candidate.schema_compliance is not None
            and candidate.schema_compliance < release.min_schema_compliance
        ):
            failures.append(
                f"schema={candidate.schema_compliance:.4f}<{release.min_schema_compliance:.4f}"
            )
        if candidate.p99_latency_ms > release.max_p99_latency_ms:
            failures.append(f"p99={candidate.p99_latency_ms:.2f}>{release.max_p99_latency_ms:.2f}")
        if 1 - candidate.pass_rate > release.max_error_rate:
            failures.append(
                f"failure_rate={1 - candidate.pass_rate:.4f}>{release.max_error_rate:.4f}"
            )
        if failures:
            raise EvaluationGateError("release rejected: " + ";".join(failures))


def _quality(case: EvaluationCase, response: GatewayResponse) -> float:
    actual = _normalize(response.text)
    checks: list[bool] = []
    if case.expected_text is not None:
        checks.append(actual == _normalize(case.expected_text))
    checks.extend(_normalize(fragment) in actual for fragment in case.expected_contains)
    if not checks:
        checks.append(bool(actual))
    return sum(checks) / len(checks)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]
