from __future__ import annotations

import pytest

from aegis_gateway.control.circuit_breaker import CircuitBreakers, CircuitState
from aegis_gateway.control.rate_limit import TokenBucketLimiter
from aegis_gateway.errors import CircuitOpenError, RateLimitExceededError


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_token_bucket_rejects_and_refills() -> None:
    clock = Clock()
    limiter = TokenBucketLimiter(rate_per_second=2, burst=2, clock=clock)
    assert await limiter.consume("tenant") == 1
    assert await limiter.consume("tenant") == 0
    with pytest.raises(RateLimitExceededError, match="retry after"):
        await limiter.consume("tenant")
    clock.now = 0.5
    assert await limiter.consume("tenant") == 0


async def test_token_bucket_configuration_is_validated() -> None:
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate_per_second=0, burst=1)
    limiter = TokenBucketLimiter(rate_per_second=1, burst=1)
    with pytest.raises(ValueError):
        await limiter.consume("tenant", 2)


async def test_circuit_opens_half_opens_and_closes() -> None:
    clock = Clock()
    circuits = CircuitBreakers(failure_threshold=2, recovery_seconds=10, clock=clock)
    await circuits.before_request("route")
    await circuits.record_failure("route")
    assert await circuits.is_available("route")
    await circuits.record_failure("route")
    assert not await circuits.is_available("route")
    with pytest.raises(CircuitOpenError):
        await circuits.before_request("route")
    clock.now = 10
    assert await circuits.is_available("route")
    await circuits.before_request("route")
    with pytest.raises(CircuitOpenError, match="probe"):
        await circuits.before_request("route")
    await circuits.record_success("route")
    snapshot = (await circuits.snapshots())[0]
    assert snapshot.state is CircuitState.CLOSED
    assert snapshot.consecutive_failures == 0


async def test_half_open_failure_reopens() -> None:
    clock = Clock()
    circuits = CircuitBreakers(failure_threshold=1, recovery_seconds=1, clock=clock)
    await circuits.record_failure("route")
    clock.now = 1
    await circuits.before_request("route")
    await circuits.record_failure("route")
    assert (await circuits.snapshots())[0].state is CircuitState.OPEN
