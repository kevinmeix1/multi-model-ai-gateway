"""Per-route circuit breakers with one-probe half-open recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from aegis_gateway.errors import CircuitOpenError


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitSnapshot:
    route_id: str
    state: CircuitState
    consecutive_failures: int
    opened_at: float | None


@dataclass
class _Circuit:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


class CircuitBreakers:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 30,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if failure_threshold <= 0 or recovery_seconds <= 0:
            raise ValueError("circuit settings must be positive")
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}
        self._lock = asyncio.Lock()

    async def is_available(self, route_id: str) -> bool:
        async with self._lock:
            circuit = self._circuits.setdefault(route_id, _Circuit())
            self._advance(circuit)
            return circuit.state is CircuitState.CLOSED or (
                circuit.state is CircuitState.HALF_OPEN and not circuit.probe_in_flight
            )

    async def before_request(self, route_id: str) -> None:
        async with self._lock:
            circuit = self._circuits.setdefault(route_id, _Circuit())
            self._advance(circuit)
            if circuit.state is CircuitState.OPEN:
                raise CircuitOpenError(f"circuit for route '{route_id}' is open")
            if circuit.state is CircuitState.HALF_OPEN:
                if circuit.probe_in_flight:
                    raise CircuitOpenError(f"circuit for route '{route_id}' has a probe in flight")
                circuit.probe_in_flight = True

    async def record_success(self, route_id: str) -> None:
        async with self._lock:
            circuit = self._circuits.setdefault(route_id, _Circuit())
            circuit.state = CircuitState.CLOSED
            circuit.consecutive_failures = 0
            circuit.opened_at = None
            circuit.probe_in_flight = False

    async def record_failure(self, route_id: str) -> None:
        async with self._lock:
            circuit = self._circuits.setdefault(route_id, _Circuit())
            circuit.probe_in_flight = False
            circuit.consecutive_failures += 1
            if (
                circuit.state is CircuitState.HALF_OPEN
                or circuit.consecutive_failures >= self._threshold
            ):
                circuit.state = CircuitState.OPEN
                circuit.opened_at = self._clock()

    async def snapshots(self) -> list[CircuitSnapshot]:
        async with self._lock:
            result = []
            for route_id, circuit in self._circuits.items():
                self._advance(circuit)
                result.append(
                    CircuitSnapshot(
                        route_id=route_id,
                        state=circuit.state,
                        consecutive_failures=circuit.consecutive_failures,
                        opened_at=circuit.opened_at,
                    )
                )
            return result

    def _advance(self, circuit: _Circuit) -> None:
        if (
            circuit.state is CircuitState.OPEN
            and circuit.opened_at is not None
            and self._clock() - circuit.opened_at >= self._recovery
        ):
            circuit.state = CircuitState.HALF_OPEN
            circuit.probe_in_flight = False
