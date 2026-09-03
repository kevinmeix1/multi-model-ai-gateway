"""Online data plane coordinating policy, providers, resilience, and evidence."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from jsonschema import ValidationError, validate

from aegis_gateway.control.cache import SemanticCache
from aegis_gateway.control.circuit_breaker import CircuitBreakers
from aegis_gateway.control.rate_limit import TokenBucketLimiter
from aegis_gateway.control.registry import ArtifactRegistry
from aegis_gateway.control.release import ReleaseAssignment, ReleaseManager
from aegis_gateway.control.router import PolicyRouter, actual_cost
from aegis_gateway.control.telemetry import Telemetry
from aegis_gateway.domain import (
    ArtifactKind,
    CacheMode,
    GatewayRequest,
    GatewayResponse,
    GatewayStreamEvent,
    GatewayUsage,
    Message,
    ModelRoute,
    RequestMetric,
    RoutingDecision,
)
from aegis_gateway.errors import (
    AegisError,
    ProviderError,
    SchemaViolationError,
    StreamInterruptedError,
)
from aegis_gateway.providers.base import ProviderRegistry

_VARIABLE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


class GatewayService:
    def __init__(
        self,
        *,
        router: PolicyRouter,
        providers: ProviderRegistry,
        limiter: TokenBucketLimiter,
        circuits: CircuitBreakers,
        cache: SemanticCache,
        artifacts: ArtifactRegistry,
        releases: ReleaseManager,
        telemetry: Telemetry,
    ) -> None:
        self.router = router
        self.providers = providers
        self.limiter = limiter
        self.circuits = circuits
        self.cache = cache
        self.artifacts = artifacts
        self.releases = releases
        self.telemetry = telemetry
        self._background: set[asyncio.Task[Any]] = set()

    async def generate(
        self,
        request: GatewayRequest,
        *,
        forced_route_id: str | None = None,
        evaluation: bool = False,
        shadow: bool = False,
        assignment_override: ReleaseAssignment | None = None,
    ) -> GatewayResponse:
        request = await self._resolve_prompt(request)
        started = monotonic()
        if not evaluation and not shadow:
            await self.limiter.consume(request.tenant_id)
        if (
            not evaluation
            and not shadow
            and request.cache_mode is CacheMode.DEFAULT
            and (cached := await self.cache.get(request)) is not None
        ):
            elapsed = (monotonic() - started) * 1000
            cached = cached.model_copy(
                update={
                    "request_id": request.request_id,
                    "cache_hit": True,
                    "ttft_ms": elapsed,
                    "latency_ms": elapsed,
                    "usage": GatewayUsage(input_tokens=0, output_tokens=0, cost_usd=0),
                    "created_at": datetime.now(UTC),
                }
            )
            await self._record_response(
                request=request,
                response=cached,
                assignment=ReleaseAssignment(),
                shadow=False,
            )
            return cached

        if assignment_override is not None:
            assignment = assignment_override
        elif evaluation or shadow or forced_route_id is not None:
            assignment = ReleaseAssignment()
        else:
            assignment = await self.releases.assignment(request)
        decision = await self.router.decide(
            request,
            preferred_route_id=assignment.preferred_route_id,
            forced_route_id=forced_route_id,
            canary=assignment.canary,
            release_id=assignment.release_id,
        )
        ordered = _ordered_candidates(decision)
        fallback_count = 0
        last_error: AegisError | None = None

        for candidate in ordered:
            route = self.router.get_route(candidate.route_id)
            try:
                await self.circuits.before_request(route.id)
                result = await self.providers.get(route.provider).complete(request, route)
                parsed, schema_valid = _validate_schema(request, result.text)
                await self.circuits.record_success(route.id)
                response = GatewayResponse(
                    request_id=request.request_id,
                    route_id=route.id,
                    provider=route.provider,
                    model=result.raw_model,
                    text=result.text,
                    parsed=parsed,
                    schema_valid=schema_valid,
                    usage=GatewayUsage(
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        cost_usd=actual_cost(route, result.input_tokens, result.output_tokens),
                    ),
                    ttft_ms=result.ttft_ms,
                    latency_ms=(monotonic() - started) * 1000,
                    fallback_count=fallback_count,
                    routing_regret=candidate.routing_regret,
                )
                if not evaluation:
                    await self._record_response(
                        request=request,
                        response=response,
                        assignment=assignment,
                        shadow=shadow,
                    )
                if not evaluation and not shadow and request.cache_mode is not CacheMode.BYPASS:
                    await self.cache.put(request, response)
                self._launch_post_response(request, assignment, shadow)
                return response
            except SchemaViolationError as exc:
                last_error = exc
                fallback_count += 1
                self.telemetry.event(
                    "route_schema_failure",
                    request_id=request.request_id,
                    route_id=route.id,
                    fallback_count=fallback_count,
                )
            except ProviderError as exc:
                last_error = exc
                fallback_count += 1
                await self.circuits.record_failure(route.id)
                self.telemetry.event(
                    "route_provider_failure",
                    request_id=request.request_id,
                    route_id=route.id,
                    provider=route.provider,
                    error_code=exc.code,
                    fallback_count=fallback_count,
                )
                if exc.code == "invalid_provider_request":
                    break

        assert last_error is not None
        if not evaluation:
            route = self.router.get_route(ordered[-1].route_id)
            await self._record_error(
                request=request,
                route=route,
                error=last_error,
                fallback_count=fallback_count,
                assignment=assignment,
                shadow=shadow,
                latency_ms=(monotonic() - started) * 1000,
            )
        raise last_error

    async def stream(
        self, request: GatewayRequest, *, forced_route_id: str | None = None
    ) -> AsyncIterator[GatewayStreamEvent]:
        request = await self._resolve_prompt(request)
        started = monotonic()
        await self.limiter.consume(request.tenant_id)
        if request.cache_mode is CacheMode.DEFAULT:
            cached = await self.cache.get(request)
            if cached is not None:
                elapsed = (monotonic() - started) * 1000
                cached = cached.model_copy(
                    update={
                        "request_id": request.request_id,
                        "cache_hit": True,
                        "ttft_ms": elapsed,
                        "latency_ms": elapsed,
                        "usage": GatewayUsage(input_tokens=0, output_tokens=0, cost_usd=0),
                    }
                )
                yield GatewayStreamEvent(
                    type="start",
                    request_id=request.request_id,
                    route_id=cached.route_id,
                    provider=cached.provider,
                    model=cached.model,
                )
                yield GatewayStreamEvent(
                    type="delta",
                    request_id=request.request_id,
                    route_id=cached.route_id,
                    provider=cached.provider,
                    model=cached.model,
                    delta=cached.text,
                )
                await self._record_response(
                    request=request,
                    response=cached,
                    assignment=ReleaseAssignment(),
                    shadow=False,
                )
                yield GatewayStreamEvent(
                    type="done",
                    request_id=request.request_id,
                    route_id=cached.route_id,
                    provider=cached.provider,
                    model=cached.model,
                    response=cached,
                )
                return

        assignment = (
            ReleaseAssignment()
            if forced_route_id is not None
            else await self.releases.assignment(request)
        )
        decision = await self.router.decide(
            request,
            preferred_route_id=assignment.preferred_route_id,
            forced_route_id=forced_route_id,
            canary=assignment.canary,
            release_id=assignment.release_id,
        )
        ordered = _ordered_candidates(decision)
        fallback_count = 0
        last_error: AegisError | None = None
        self._launch_shadow(request, assignment)

        for candidate in ordered:
            route = self.router.get_route(candidate.route_id)
            emitted = False
            first_delta_at: float | None = None
            text_parts: list[str] = []
            input_tokens = 0
            output_tokens = 0
            raw_model = route.model
            try:
                await self.circuits.before_request(route.id)
                async for event in self.providers.get(route.provider).stream(request, route):
                    if event.type == "start":
                        yield GatewayStreamEvent(
                            type="start",
                            request_id=request.request_id,
                            route_id=route.id,
                            provider=route.provider,
                            model=raw_model,
                        )
                    elif event.type == "delta":
                        if not emitted:
                            first_delta_at = monotonic()
                        emitted = True
                        text_parts.append(event.delta)
                        yield GatewayStreamEvent(
                            type="delta",
                            request_id=request.request_id,
                            route_id=route.id,
                            provider=route.provider,
                            model=raw_model,
                            delta=event.delta,
                        )
                    elif event.type == "usage":
                        input_tokens = event.input_tokens or 0
                        output_tokens = event.output_tokens or 0
                text = "".join(text_parts)
                parsed, schema_valid = _validate_schema(request, text)
                await self.circuits.record_success(route.id)
                completed_at = monotonic()
                response = GatewayResponse(
                    request_id=request.request_id,
                    route_id=route.id,
                    provider=route.provider,
                    model=raw_model,
                    text=text,
                    parsed=parsed,
                    schema_valid=schema_valid,
                    usage=GatewayUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=actual_cost(route, input_tokens, output_tokens),
                    ),
                    ttft_ms=((first_delta_at or completed_at) - started) * 1000,
                    latency_ms=(completed_at - started) * 1000,
                    fallback_count=fallback_count,
                    routing_regret=candidate.routing_regret,
                )
                await self._record_response(
                    request=request,
                    response=response,
                    assignment=assignment,
                    shadow=False,
                )
                if request.cache_mode is not CacheMode.BYPASS:
                    await self.cache.put(request, response)
                self._launch_release_assessment(assignment)
                yield GatewayStreamEvent(
                    type="done",
                    request_id=request.request_id,
                    route_id=route.id,
                    provider=route.provider,
                    model=raw_model,
                    response=response,
                )
                return
            except SchemaViolationError as exc:
                last_error = exc
                if emitted:
                    await self._record_error(
                        request=request,
                        route=route,
                        error=exc,
                        fallback_count=fallback_count,
                        assignment=assignment,
                        shadow=False,
                        latency_ms=(monotonic() - started) * 1000,
                    )
                    raise
                fallback_count += 1
            except ProviderError as exc:
                last_error = exc
                await self.circuits.record_failure(route.id)
                if emitted:
                    interrupted = StreamInterruptedError(str(exc), provider=route.provider)
                    await self._record_error(
                        request=request,
                        route=route,
                        error=interrupted,
                        fallback_count=fallback_count,
                        assignment=assignment,
                        shadow=False,
                        latency_ms=(monotonic() - started) * 1000,
                    )
                    raise interrupted from exc
                fallback_count += 1
                if exc.code == "invalid_provider_request":
                    break

        assert last_error is not None
        route = self.router.get_route(ordered[-1].route_id)
        await self._record_error(
            request=request,
            route=route,
            error=last_error,
            fallback_count=fallback_count,
            assignment=assignment,
            shadow=False,
            latency_ms=(monotonic() - started) * 1000,
        )
        raise last_error

    async def _resolve_prompt(self, request: GatewayRequest) -> GatewayRequest:
        if request.prompt is None:
            return request
        artifact = await self.artifacts.get(
            kind=ArtifactKind.PROMPT,
            name=request.prompt.name,
            version=request.prompt.version,
        )
        template = artifact.content.get("template")
        if not isinstance(template, str) or not template:
            raise ValueError("prompt artifact must contain a non-empty template")
        required = set(_VARIABLE.findall(template))
        missing = required - request.prompt_variables.keys()
        if missing:
            raise ValueError(f"missing prompt variables: {','.join(sorted(missing))}")

        def replace(match: re.Match[str]) -> str:
            return request.prompt_variables[match.group(1)]

        rendered = _VARIABLE.sub(replace, template)
        return request.model_copy(
            update={"messages": [Message(role="developer", content=rendered), *request.messages]}
        )

    async def _record_response(
        self,
        *,
        request: GatewayRequest,
        response: GatewayResponse,
        assignment: ReleaseAssignment,
        shadow: bool,
    ) -> None:
        await self.telemetry.record(
            RequestMetric(
                request_id=request.request_id,
                tenant_id=request.tenant_id,
                route_id=response.route_id,
                provider=response.provider,
                success=True,
                schema_valid=response.schema_valid,
                cache_hit=response.cache_hit,
                canary=assignment.canary,
                release_id=assignment.release_id,
                shadow=shadow,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=response.usage.cost_usd,
                ttft_ms=response.ttft_ms,
                latency_ms=response.latency_ms,
                routing_regret=response.routing_regret,
                fallback_count=response.fallback_count,
            )
        )

    async def _record_error(
        self,
        *,
        request: GatewayRequest,
        route: ModelRoute,
        error: AegisError,
        fallback_count: int,
        assignment: ReleaseAssignment,
        shadow: bool,
        latency_ms: float,
    ) -> None:
        await self.telemetry.record(
            RequestMetric(
                request_id=request.request_id,
                tenant_id=request.tenant_id,
                route_id=route.id,
                provider=route.provider,
                success=False,
                schema_valid=False if request.response_schema else None,
                cache_hit=False,
                canary=assignment.canary,
                release_id=assignment.release_id,
                shadow=shadow,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0,
                ttft_ms=latency_ms,
                latency_ms=latency_ms,
                routing_regret=0,
                fallback_count=fallback_count,
                error_code=error.code,
            )
        )

    def _launch_post_response(
        self,
        request: GatewayRequest,
        assignment: ReleaseAssignment,
        shadow: bool,
    ) -> None:
        if not shadow:
            self._launch_shadow(request, assignment)
            self._launch_release_assessment(assignment)

    def _launch_shadow(self, request: GatewayRequest, assignment: ReleaseAssignment) -> None:
        if assignment.shadow_route_id is None:
            return
        shadow_request = request.model_copy(
            update={
                "request_id": f"{request.request_id}-shadow",
                "stream": False,
                "cache_mode": CacheMode.BYPASS,
                "shadow_enabled": False,
            }
        )
        task = asyncio.create_task(
            self.generate(
                shadow_request,
                forced_route_id=assignment.shadow_route_id,
                shadow=True,
                assignment_override=ReleaseAssignment(release_id=assignment.release_id),
            )
        )
        self._track(task)

    def _launch_release_assessment(self, assignment: ReleaseAssignment) -> None:
        if not assignment.canary or assignment.release_id is None:
            return
        task = asyncio.create_task(self.releases.assess(assignment.release_id))
        self._track(task)

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def aclose(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        await self.providers.aclose()


def _ordered_candidates(decision: RoutingDecision) -> list[Any]:
    selected = next(
        candidate
        for candidate in decision.candidates
        if candidate.route_id == decision.selected_route_id
    )
    return [selected, *(item for item in decision.candidates if item is not selected)]


def _validate_schema(request: GatewayRequest, text: str) -> tuple[Any | None, bool | None]:
    if request.response_schema is None:
        return None, None
    try:
        parsed = json.loads(text)
        validate(instance=parsed, schema=request.response_schema)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SchemaViolationError("provider output did not satisfy the response schema") from exc
    return parsed, True
