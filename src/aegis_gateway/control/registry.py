"""SQLite-backed immutable artifacts, releases, evaluations, and request evidence."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import aiosqlite

from aegis_gateway.domain import (
    Artifact,
    ArtifactKind,
    EvaluationRun,
    MetricsSnapshot,
    Release,
    ReleaseState,
    RequestMetric,
)
from aegis_gateway.errors import (
    ArtifactNotFoundError,
    RegistryConflictError,
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (kind, name, version)
                );

                CREATE TABLE IF NOT EXISTS releases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    baseline_route_id TEXT NOT NULL,
                    candidate_route_id TEXT NOT NULL,
                    canary_percent INTEGER NOT NULL,
                    shadow_percent INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    max_error_rate REAL NOT NULL,
                    max_p99_latency_ms REAL NOT NULL,
                    min_schema_compliance REAL NOT NULL,
                    min_quality_score REAL NOT NULL,
                    min_canary_samples INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_releases_name_state
                    ON releases(name, state, updated_at);

                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY,
                    dataset_name TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS request_metrics (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    schema_valid INTEGER,
                    cache_hit INTEGER NOT NULL,
                    canary INTEGER NOT NULL,
                    release_id TEXT,
                    shadow INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    ttft_ms REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    routing_regret REAL NOT NULL,
                    fallback_count INTEGER NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_metrics_created
                    ON request_metrics(created_at);
                CREATE INDEX IF NOT EXISTS idx_metrics_release
                    ON request_metrics(release_id, canary, created_at);

                CREATE TABLE IF NOT EXISTS rollback_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            await db.commit()


class ArtifactRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def put(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        version: str,
        content: dict[str, Any],
    ) -> Artifact:
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        content_hash = sha256(canonical.encode()).hexdigest()
        created_at = datetime.now(UTC)
        try:
            async with aiosqlite.connect(self._database.path) as db:
                await db.execute(
                    """
                    INSERT INTO artifacts(
                        kind, name, version, content_json, content_hash, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (kind.value, name, version, canonical, content_hash, created_at.isoformat()),
                )
                await db.commit()
        except aiosqlite.IntegrityError:
            existing = await self.get(kind=kind, name=name, version=version)
            if existing.content_hash == content_hash:
                return existing
            raise RegistryConflictError(
                f"artifact {kind}/{name}/{version} already exists with different content"
            ) from None
        return Artifact(
            kind=kind,
            name=name,
            version=version,
            content=content,
            content_hash=content_hash,
            created_at=created_at,
        )

    async def get(self, *, kind: ArtifactKind, name: str, version: str | None = None) -> Artifact:
        if version is None:
            query = """
                SELECT kind, name, version, content_json, content_hash, created_at
                FROM artifacts WHERE kind = ? AND name = ?
                ORDER BY created_at DESC LIMIT 1
            """
            params: tuple[str, ...] = (kind.value, name)
        else:
            query = """
                SELECT kind, name, version, content_json, content_hash, created_at
                FROM artifacts WHERE kind = ? AND name = ? AND version = ?
            """
            params = (kind.value, name, version)
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute(query, params)
            row = await cursor.fetchone()
        if row is None:
            suffix = version or "latest"
            raise ArtifactNotFoundError(f"artifact {kind}/{name}/{suffix} was not found")
        return _artifact_from_row(row)

    async def list(self, *, kind: ArtifactKind | None = None) -> list[Artifact]:
        query = """
            SELECT kind, name, version, content_json, content_hash, created_at FROM artifacts
        """
        params: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            params = (kind.value,)
        query += " ORDER BY kind, name, created_at DESC"
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [_artifact_from_row(row) for row in rows]


class ReleaseRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, release: Release) -> Release:
        try:
            async with aiosqlite.connect(self._database.path) as db:
                await db.execute(
                    """
                    INSERT INTO releases(
                        id, name, baseline_route_id, candidate_route_id, canary_percent,
                        shadow_percent, state, max_error_rate, max_p99_latency_ms,
                        min_schema_compliance, min_quality_score, min_canary_samples,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        release.id,
                        release.name,
                        release.baseline_route_id,
                        release.candidate_route_id,
                        release.canary_percent,
                        release.shadow_percent,
                        release.state.value,
                        release.max_error_rate,
                        release.max_p99_latency_ms,
                        release.min_schema_compliance,
                        release.min_quality_score,
                        release.min_canary_samples,
                        release.created_at.isoformat(),
                        release.updated_at.isoformat(),
                    ),
                )
                await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise RegistryConflictError(f"release '{release.id}' already exists") from exc
        return release

    async def get(self, release_id: str) -> Release:
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute("SELECT * FROM releases WHERE id = ?", (release_id,))
            row = await cursor.fetchone()
        if row is None:
            raise ArtifactNotFoundError(f"release '{release_id}' was not found")
        return _release_from_row(row)

    async def get_live(self, name: str = "default") -> Release | None:
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute(
                """
                SELECT * FROM releases
                WHERE name = ? AND state IN (?, ?)
                ORDER BY CASE state WHEN ? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (
                    name,
                    ReleaseState.CANDIDATE.value,
                    ReleaseState.ACTIVE.value,
                    ReleaseState.CANDIDATE.value,
                ),
            )
            row = await cursor.fetchone()
        return _release_from_row(row) if row is not None else None

    async def set_state(self, release_id: str, state: ReleaseState) -> Release:
        now = datetime.now(UTC)
        async with aiosqlite.connect(self._database.path) as db:
            if state is ReleaseState.ACTIVE:
                current = await self.get(release_id)
                await db.execute(
                    """
                    UPDATE releases SET state = ?, updated_at = ?
                    WHERE name = ? AND state = ? AND id != ?
                    """,
                    (
                        ReleaseState.ROLLED_BACK.value,
                        now.isoformat(),
                        current.name,
                        ReleaseState.ACTIVE.value,
                        release_id,
                    ),
                )
            cursor = await db.execute(
                "UPDATE releases SET state = ?, updated_at = ? WHERE id = ?",
                (state.value, now.isoformat(), release_id),
            )
            if cursor.rowcount != 1:
                raise ArtifactNotFoundError(f"release '{release_id}' was not found")
            await db.commit()
        return await self.get(release_id)

    async def list(self) -> list[Release]:
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute("SELECT * FROM releases ORDER BY updated_at DESC")
            rows = await cursor.fetchall()
        return [_release_from_row(row) for row in rows]

    async def record_rollback(self, release_id: str, reason: str, duration_ms: float) -> None:
        async with aiosqlite.connect(self._database.path) as db:
            await db.execute(
                """
                INSERT INTO rollback_events(release_id, reason, duration_ms, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (release_id, reason, duration_ms, datetime.now(UTC).isoformat()),
            )
            await db.commit()


class EvidenceStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_metric(self, metric: RequestMetric) -> None:
        async with aiosqlite.connect(self._database.path) as db:
            await db.execute(
                """
                INSERT INTO request_metrics(
                    request_id, tenant_id, route_id, provider, success, schema_valid,
                    cache_hit, canary, release_id, shadow, input_tokens, output_tokens,
                    cost_usd, ttft_ms, latency_ms, routing_regret, fallback_count,
                    error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric.request_id,
                    metric.tenant_id,
                    metric.route_id,
                    metric.provider,
                    int(metric.success),
                    None if metric.schema_valid is None else int(metric.schema_valid),
                    int(metric.cache_hit),
                    int(metric.canary),
                    metric.release_id,
                    int(metric.shadow),
                    metric.input_tokens,
                    metric.output_tokens,
                    metric.cost_usd,
                    metric.ttft_ms,
                    metric.latency_ms,
                    metric.routing_regret,
                    metric.fallback_count,
                    metric.error_code,
                    metric.created_at.isoformat(),
                ),
            )
            await db.commit()

    async def metrics(
        self,
        *,
        release_id: str | None = None,
        route_id: str | None = None,
        canary: bool | None = None,
        limit: int = 10_000,
    ) -> list[RequestMetric]:
        clauses: list[str] = []
        params: list[Any] = []
        if release_id is not None:
            clauses.append("release_id = ?")
            params.append(release_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        if canary is not None:
            clauses.append("canary = ?")
            params.append(int(canary))
        query = "SELECT * FROM request_metrics"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [_metric_from_row(row) for row in rows]

    async def summary(
        self,
        *,
        release_id: str | None = None,
        route_id: str | None = None,
        canary: bool | None = None,
    ) -> MetricsSnapshot:
        metrics = await self.metrics(
            release_id=release_id, route_id=route_id, canary=canary, limit=100_000
        )
        return summarize_metrics(metrics)

    async def put_evaluation(self, run: EvaluationRun) -> None:
        async with aiosqlite.connect(self._database.path) as db:
            await db.execute(
                """
                INSERT INTO evaluation_runs(
                    id, dataset_name, dataset_version, route_id, run_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.dataset_name,
                    run.dataset_version,
                    run.route_id,
                    run.model_dump_json(),
                    run.created_at.isoformat(),
                ),
            )
            await db.commit()

    async def list_evaluations(self, limit: int = 100) -> list[EvaluationRun]:
        async with aiosqlite.connect(self._database.path) as db:
            cursor = await db.execute(
                "SELECT run_json FROM evaluation_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [EvaluationRun.model_validate_json(row[0]) for row in rows]


def summarize_metrics(metrics: list[RequestMetric]) -> MetricsSnapshot:
    count = len(metrics)
    successes = [metric for metric in metrics if metric.success]
    schemas = [metric.schema_valid for metric in metrics if metric.schema_valid is not None]
    failovers = [metric for metric in metrics if metric.fallback_count > 0]
    latencies = sorted(metric.latency_ms for metric in metrics)
    return MetricsSnapshot(
        requests=count,
        successful_requests=len(successes),
        success_rate=len(successes) / count if count else 0.0,
        schema_compliance=(sum(bool(value) for value in schemas) / len(schemas))
        if schemas
        else None,
        cache_hit_rate=sum(metric.cache_hit for metric in metrics) / count if count else 0.0,
        failover_success_rate=(sum(metric.success for metric in failovers) / len(failovers))
        if failovers
        else None,
        mean_cost_per_success_usd=(sum(metric.cost_usd for metric in successes) / len(successes))
        if successes
        else None,
        mean_ttft_ms=(sum(metric.ttft_ms for metric in metrics) / count) if count else None,
        p99_latency_ms=_percentile(latencies, 0.99),
        mean_routing_regret=(sum(metric.routing_regret for metric in metrics) / count)
        if count
        else None,
    )


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(quantile * len(values)) - 1)
    return values[index]


def _artifact_from_row(row: Sequence[Any]) -> Artifact:
    return Artifact(
        kind=ArtifactKind(row[0]),
        name=row[1],
        version=row[2],
        content=json.loads(row[3]),
        content_hash=row[4],
        created_at=datetime.fromisoformat(row[5]),
    )


def _release_from_row(row: Sequence[Any]) -> Release:
    return Release(
        id=row[0],
        name=row[1],
        baseline_route_id=row[2],
        candidate_route_id=row[3],
        canary_percent=row[4],
        shadow_percent=row[5],
        state=ReleaseState(row[6]),
        max_error_rate=row[7],
        max_p99_latency_ms=row[8],
        min_schema_compliance=row[9],
        min_quality_score=row[10],
        min_canary_samples=row[11],
        created_at=datetime.fromisoformat(row[12]),
        updated_at=datetime.fromisoformat(row[13]),
    )


def _metric_from_row(row: Sequence[Any]) -> RequestMetric:
    return RequestMetric(
        request_id=row[1],
        tenant_id=row[2],
        route_id=row[3],
        provider=row[4],
        success=bool(row[5]),
        schema_valid=None if row[6] is None else bool(row[6]),
        cache_hit=bool(row[7]),
        canary=bool(row[8]),
        release_id=row[9],
        shadow=bool(row[10]),
        input_tokens=row[11],
        output_tokens=row[12],
        cost_usd=row[13],
        ttft_ms=row[14],
        latency_ms=row[15],
        routing_regret=row[16],
        fallback_count=row[17],
        error_code=row[18],
        created_at=datetime.fromisoformat(row[19]),
    )
