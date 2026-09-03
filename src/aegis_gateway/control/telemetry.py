"""Durable request evidence plus bounded-cardinality Prometheus metrics."""

from __future__ import annotations

import json
import logging
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from aegis_gateway.control.registry import EvidenceStore
from aegis_gateway.domain import RequestMetric


class Telemetry:
    def __init__(self, evidence: EvidenceStore) -> None:
        self._evidence = evidence
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "aegis_requests_total",
            "Gateway attempts by stable route labels",
            ["provider", "route", "outcome", "canary", "shadow"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "aegis_request_latency_seconds",
            "End-to-end gateway latency",
            ["provider", "route"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
            registry=self.registry,
        )
        self.ttft = Histogram(
            "aegis_ttft_seconds",
            "Time to first output token",
            ["provider", "route"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.cost = Counter(
            "aegis_cost_usd_total",
            "Estimated provider cost in USD",
            ["provider", "route"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "aegis_tokens_total",
            "Provider-reported tokens",
            ["provider", "route", "direction"],
            registry=self.registry,
        )
        self.cache_hits = Counter(
            "aegis_cache_hits_total",
            "Semantic-cache hits",
            registry=self.registry,
        )
        self.circuit_state = Gauge(
            "aegis_circuit_state",
            "Circuit state encoded as closed=0, half_open=1, open=2",
            ["route"],
            registry=self.registry,
        )
        self._logger = logging.getLogger("aegis.request")

    async def record(self, metric: RequestMetric) -> None:
        await self._evidence.record_metric(metric)
        labels = {
            "provider": metric.provider,
            "route": metric.route_id,
            "outcome": "success" if metric.success else metric.error_code or "error",
            "canary": str(metric.canary).lower(),
            "shadow": str(metric.shadow).lower(),
        }
        self.requests.labels(**labels).inc()
        self.latency.labels(provider=metric.provider, route=metric.route_id).observe(
            metric.latency_ms / 1000
        )
        self.ttft.labels(provider=metric.provider, route=metric.route_id).observe(
            metric.ttft_ms / 1000
        )
        self.cost.labels(provider=metric.provider, route=metric.route_id).inc(metric.cost_usd)
        self.tokens.labels(provider=metric.provider, route=metric.route_id, direction="input").inc(
            metric.input_tokens
        )
        self.tokens.labels(provider=metric.provider, route=metric.route_id, direction="output").inc(
            metric.output_tokens
        )
        if metric.cache_hit:
            self.cache_hits.inc()
        self._logger.info(
            json.dumps(
                {
                    "event": "gateway_request_completed",
                    **metric.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )

    def render_prometheus(self) -> bytes:
        return generate_latest(self.registry)

    def event(self, event: str, **attributes: Any) -> None:
        safe = {key: value for key, value in attributes.items() if "key" not in key.casefold()}
        self._logger.info(json.dumps({"event": event, **safe}, default=str, sort_keys=True))
