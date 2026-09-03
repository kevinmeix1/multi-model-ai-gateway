from __future__ import annotations

from collections.abc import Callable

import pytest

from aegis_gateway.control.circuit_breaker import CircuitBreakers
from aegis_gateway.control.router import PolicyRouter, UtilityWeights, actual_cost
from aegis_gateway.domain import (
    Capability,
    DataClassification,
    GatewayRequest,
    ModelRoute,
    PrivacyMode,
)
from aegis_gateway.errors import NoEligibleRouteError


def router(routes: list[ModelRoute], circuits: CircuitBreakers | None = None) -> PolicyRouter:
    return PolicyRouter(
        routes,
        circuits=circuits or CircuitBreakers(),
        policy_version="test-v1",
    )


def test_utility_weights_and_unknown_route_are_validated(
    route_factory: Callable[..., ModelRoute],
) -> None:
    with pytest.raises(ValueError, match="sum to one"):
        UtilityWeights(quality=1, latency=1, cost=1)
    value = router([route_factory()])
    assert value.get_route("route-a").model == "model-a"
    with pytest.raises(NoEligibleRouteError, match="unknown route"):
        value.get_route("missing")


async def test_router_ranks_by_utility_and_honors_eligible_preference(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    high_quality = route_factory(
        id="quality",
        quality_score=0.95,
        expected_p95_latency_ms=200,
        input_cost_per_million=2,
        output_cost_per_million=2,
    )
    fast = route_factory(id="fast", quality_score=0.75, expected_p95_latency_ms=20)
    policy = router([high_quality, fast])
    request = request_factory(max_cost_usd=1, max_latency_ms=10_000)

    default = await policy.decide(request)
    assert default.selected_route_id == "quality"
    assert default.candidates[0].routing_regret == 0
    preferred = await policy.decide(
        request,
        preferred_route_id="fast",
        canary=True,
        release_id="release-1",
    )
    assert preferred.selected_route_id == "fast"
    assert next(item for item in preferred.candidates if item.route_id == "fast").routing_regret > 0
    assert preferred.canary is True
    assert preferred.release_id == "release-1"


async def test_router_enforces_every_hard_constraint(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    routes = [
        route_factory(id="disabled", enabled=False),
        route_factory(id="capability", capabilities={Capability.TEXT}),
        route_factory(id="privacy", privacy_modes={PrivacyMode.STANDARD}),
        route_factory(id="region", regions={"us-east"}),
        route_factory(id="context", context_window=5),
        route_factory(id="cost", input_cost_per_million=1_000_000),
        route_factory(id="latency", expected_p95_latency_ms=20_000),
    ]
    request = request_factory(
        stream=True,
        privacy_mode=PrivacyMode.LOCAL_ONLY,
        allowed_regions={"eu-west"},
        max_cost_usd=0,
        max_latency_ms=100,
    )
    with pytest.raises(NoEligibleRouteError) as captured:
        await router(routes).decide(request)
    message = str(captured.value)
    for reason in (
        "disabled",
        "missing_capabilities",
        "privacy_mode",
        "region",
        "context_window",
        "cost_budget",
        "latency_budget",
    ):
        assert reason in message


async def test_router_upgrades_data_classification_privacy(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    standard = route_factory(id="standard", privacy_modes={PrivacyMode.STANDARD})
    zero = route_factory(id="zero", privacy_modes={PrivacyMode.ZERO_RETENTION})
    local = route_factory(id="local", privacy_modes={PrivacyMode.LOCAL_ONLY})
    policy = router([standard, zero, local])

    confidential = request_factory(data_classification=DataClassification.CONFIDENTIAL)
    assert (await policy.decide(confidential)).selected_route_id == "zero"
    restricted = request_factory(data_classification=DataClassification.RESTRICTED)
    assert (await policy.decide(restricted)).selected_route_id == "local"


async def test_forcing_exclusion_and_open_circuit_remain_hard_constraints(
    route_factory: Callable[..., ModelRoute],
    request_factory: Callable[..., GatewayRequest],
) -> None:
    circuits = CircuitBreakers(failure_threshold=1)
    await circuits.record_failure("open")
    policy = router(
        [route_factory(id="open"), route_factory(id="eligible"), route_factory(id="excluded")],
        circuits,
    )
    decision = await policy.decide(
        request_factory(), forced_route_id="eligible", excluded_route_ids={"excluded"}
    )
    assert decision.selected_route_id == "eligible"
    rejected = {item.route_id: item.reasons for item in decision.rejected}
    assert "circuit_open" in rejected["open"]
    assert "previous_attempt_failed" in rejected["excluded"]
    assert "not_forced_route" in rejected["excluded"]


def test_actual_cost_is_token_metered(route_factory: Callable[..., ModelRoute]) -> None:
    route = route_factory(input_cost_per_million=2, output_cost_per_million=8)
    assert actual_cost(route, 1_000, 500) == pytest.approx(0.006)
