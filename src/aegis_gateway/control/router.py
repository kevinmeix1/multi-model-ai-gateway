"""Constraint-first route filtering followed by explicit utility ranking."""

from __future__ import annotations

from dataclasses import dataclass

from aegis_gateway.control.circuit_breaker import CircuitBreakers
from aegis_gateway.domain import (
    DataClassification,
    GatewayRequest,
    ModelRoute,
    PrivacyMode,
    RejectedRoute,
    RoutingCandidate,
    RoutingDecision,
)
from aegis_gateway.errors import NoEligibleRouteError
from aegis_gateway.providers.base import request_input_tokens


@dataclass(frozen=True)
class UtilityWeights:
    quality: float = 0.55
    latency: float = 0.25
    cost: float = 0.20

    def __post_init__(self) -> None:
        if abs(self.quality + self.latency + self.cost - 1.0) > 1e-9:
            raise ValueError("utility weights must sum to one")


class PolicyRouter:
    def __init__(
        self,
        routes: list[ModelRoute],
        *,
        circuits: CircuitBreakers,
        policy_version: str,
        weights: UtilityWeights | None = None,
    ) -> None:
        self._routes = {route.id: route for route in routes}
        self._circuits = circuits
        self._policy_version = policy_version
        self._weights = weights or UtilityWeights()

    @property
    def routes(self) -> list[ModelRoute]:
        return list(self._routes.values())

    def get_route(self, route_id: str) -> ModelRoute:
        try:
            return self._routes[route_id]
        except KeyError as exc:
            raise NoEligibleRouteError(f"unknown route '{route_id}'") from exc

    async def decide(
        self,
        request: GatewayRequest,
        *,
        preferred_route_id: str | None = None,
        forced_route_id: str | None = None,
        excluded_route_ids: set[str] | None = None,
        canary: bool = False,
        release_id: str | None = None,
    ) -> RoutingDecision:
        excluded = excluded_route_ids or set()
        input_tokens = request_input_tokens(request)
        candidates: list[RoutingCandidate] = []
        rejected: list[RejectedRoute] = []
        required_privacy = _required_privacy(request)

        for route in self._routes.values():
            reasons: list[str] = []
            predicted_cost = _predicted_cost(route, input_tokens, request.max_output_tokens)
            if not route.enabled:
                reasons.append("disabled")
            if route.id in excluded:
                reasons.append("previous_attempt_failed")
            if forced_route_id is not None and route.id != forced_route_id:
                reasons.append("not_forced_route")
            missing = request.required_capabilities - route.capabilities
            if missing:
                reasons.append(f"missing_capabilities:{','.join(sorted(missing))}")
            if required_privacy not in route.privacy_modes:
                reasons.append(f"privacy_mode:{required_privacy}")
            if request.allowed_regions and not request.allowed_regions.intersection(route.regions):
                reasons.append("region")
            if input_tokens + request.max_output_tokens > route.context_window:
                reasons.append("context_window")
            if predicted_cost > request.max_cost_usd:
                reasons.append("cost_budget")
            if route.expected_p95_latency_ms > request.max_latency_ms:
                reasons.append("latency_budget")
            if not await self._circuits.is_available(route.id):
                reasons.append("circuit_open")
            if reasons:
                rejected.append(RejectedRoute(route_id=route.id, reasons=reasons))
                continue
            utility = self._utility(route, request, predicted_cost)
            candidates.append(
                RoutingCandidate(
                    route_id=route.id,
                    predicted_cost_usd=predicted_cost,
                    predicted_latency_ms=route.expected_p95_latency_ms,
                    utility=utility,
                )
            )

        if not candidates:
            summary = "; ".join(f"{item.route_id}={','.join(item.reasons)}" for item in rejected)
            raise NoEligibleRouteError(f"no route satisfies the request contract: {summary}")

        candidates.sort(key=lambda item: (-item.utility, item.predicted_cost_usd, item.route_id))
        best_utility = candidates[0].utility
        for candidate in candidates:
            candidate.routing_regret = max(0.0, best_utility - candidate.utility)

        selected = candidates[0]
        if preferred_route_id is not None:
            selected = next(
                (candidate for candidate in candidates if candidate.route_id == preferred_route_id),
                selected,
            )
        return RoutingDecision(
            request_id=request.request_id,
            selected_route_id=selected.route_id,
            candidates=candidates,
            rejected=rejected,
            policy_version=self._policy_version,
            canary=canary,
            release_id=release_id,
        )

    def _utility(self, route: ModelRoute, request: GatewayRequest, predicted_cost: float) -> float:
        latency_ratio = min(1.0, route.expected_p95_latency_ms / request.max_latency_ms)
        cost_ratio = (
            min(1.0, predicted_cost / request.max_cost_usd) if request.max_cost_usd > 0 else 0.0
        )
        return (
            self._weights.quality * route.quality_score
            - self._weights.latency * latency_ratio
            - self._weights.cost * cost_ratio
        )


def _required_privacy(request: GatewayRequest) -> PrivacyMode:
    if request.data_classification is DataClassification.RESTRICTED:
        return PrivacyMode.LOCAL_ONLY
    if (
        request.data_classification is DataClassification.CONFIDENTIAL
        and request.privacy_mode is PrivacyMode.STANDARD
    ):
        return PrivacyMode.ZERO_RETENTION
    return request.privacy_mode


def _predicted_cost(route: ModelRoute, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * route.input_cost_per_million + output_tokens * route.output_cost_per_million
    ) / 1_000_000


def actual_cost(route: ModelRoute, input_tokens: int, output_tokens: int) -> float:
    return _predicted_cost(route, input_tokens, output_tokens)
