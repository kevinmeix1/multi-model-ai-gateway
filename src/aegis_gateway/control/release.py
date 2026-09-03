"""Stable canary/shadow assignment and automated rollback decisions."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import monotonic

from aegis_gateway.control.registry import EvidenceStore, ReleaseRegistry
from aegis_gateway.domain import GatewayRequest, Release, ReleaseState


@dataclass(frozen=True)
class ReleaseAssignment:
    preferred_route_id: str | None = None
    shadow_route_id: str | None = None
    canary: bool = False
    release_id: str | None = None


@dataclass(frozen=True)
class RollbackDecision:
    rolled_back: bool
    reason: str | None = None
    duration_ms: float | None = None


class ReleaseManager:
    def __init__(self, releases: ReleaseRegistry, evidence: EvidenceStore) -> None:
        self._releases = releases
        self._evidence = evidence

    async def assignment(
        self, request: GatewayRequest, *, release_name: str = "default"
    ) -> ReleaseAssignment:
        release = await self._releases.get_live(release_name)
        if release is None:
            return ReleaseAssignment()
        if release.state is ReleaseState.ACTIVE:
            return ReleaseAssignment(
                preferred_route_id=release.candidate_route_id,
                release_id=release.id,
            )
        if release.state is not ReleaseState.CANDIDATE:
            return ReleaseAssignment()
        canary = _bucket(release.id, request.request_id, "canary") < release.canary_percent
        shadow = (
            request.shadow_enabled
            and not canary
            and _bucket(release.id, request.request_id, "shadow") < release.shadow_percent
        )
        return ReleaseAssignment(
            preferred_route_id=(
                release.candidate_route_id if canary else release.baseline_route_id
            ),
            shadow_route_id=release.candidate_route_id if shadow else None,
            canary=canary,
            release_id=release.id,
        )

    async def assess(self, release_id: str) -> RollbackDecision:
        release = await self._releases.get(release_id)
        if release.state is not ReleaseState.CANDIDATE:
            return RollbackDecision(False)
        summary = await self._evidence.summary(release_id=release.id, canary=True)
        if summary.requests < release.min_canary_samples:
            return RollbackDecision(False)
        reasons: list[str] = []
        if 1 - summary.success_rate > release.max_error_rate:
            reasons.append(
                f"error_rate={1 - summary.success_rate:.4f}>{release.max_error_rate:.4f}"
            )
        if (
            summary.p99_latency_ms is not None
            and summary.p99_latency_ms > release.max_p99_latency_ms
        ):
            reasons.append(f"p99_ms={summary.p99_latency_ms:.2f}>{release.max_p99_latency_ms:.2f}")
        if (
            summary.schema_compliance is not None
            and summary.schema_compliance < release.min_schema_compliance
        ):
            reasons.append(
                "schema_compliance="
                f"{summary.schema_compliance:.4f}<{release.min_schema_compliance:.4f}"
            )
        if not reasons:
            return RollbackDecision(False)
        started = monotonic()
        await self._releases.set_state(release.id, ReleaseState.ROLLED_BACK)
        duration_ms = (monotonic() - started) * 1000
        reason = ";".join(reasons)
        await self._releases.record_rollback(release.id, reason, duration_ms)
        return RollbackDecision(True, reason, duration_ms)

    async def promote(self, release_id: str) -> Release:
        return await self._releases.set_state(release_id, ReleaseState.ACTIVE)

    async def start_canary(self, release_id: str) -> Release:
        return await self._releases.set_state(release_id, ReleaseState.CANDIDATE)

    async def rollback(self, release_id: str, reason: str = "manual") -> RollbackDecision:
        started = monotonic()
        await self._releases.set_state(release_id, ReleaseState.ROLLED_BACK)
        duration_ms = (monotonic() - started) * 1000
        await self._releases.record_rollback(release_id, reason, duration_ms)
        return RollbackDecision(True, reason, duration_ms)


def _bucket(release_id: str, request_id: str, lane: str) -> int:
    digest = sha256(f"{release_id}:{lane}:{request_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100
