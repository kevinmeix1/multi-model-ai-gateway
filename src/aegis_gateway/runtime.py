"""Composition root for the gateway data plane and control plane."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aegis_gateway.config import Settings, load_model_catalog
from aegis_gateway.control.cache import SemanticCache
from aegis_gateway.control.circuit_breaker import CircuitBreakers
from aegis_gateway.control.evaluation import EvaluationRunner
from aegis_gateway.control.rate_limit import TokenBucketLimiter
from aegis_gateway.control.registry import (
    ArtifactRegistry,
    Database,
    EvidenceStore,
    ReleaseRegistry,
)
from aegis_gateway.control.release import ReleaseManager
from aegis_gateway.control.router import PolicyRouter
from aegis_gateway.control.service import GatewayService
from aegis_gateway.control.telemetry import Telemetry
from aegis_gateway.providers import (
    AnthropicAdapter,
    MockAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    ProviderRegistry,
)


@dataclass
class Runtime:
    settings: Settings
    database: Database
    artifacts: ArtifactRegistry
    release_registry: ReleaseRegistry
    evidence: EvidenceStore
    release_manager: ReleaseManager
    service: GatewayService
    evaluations: EvaluationRunner

    async def initialize(self) -> None:
        await self.database.initialize()

    async def aclose(self) -> None:
        await self.service.aclose()


def create_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    routes = load_model_catalog(settings.model_catalog_path)
    database = Database(settings.database_path)
    artifacts = ArtifactRegistry(database)
    release_registry = ReleaseRegistry(database)
    evidence = EvidenceStore(database)
    release_manager = ReleaseManager(release_registry, evidence)
    circuits = CircuitBreakers(
        failure_threshold=settings.circuit_failure_threshold,
        recovery_seconds=settings.circuit_recovery_seconds,
    )
    router = PolicyRouter(
        routes,
        circuits=circuits,
        policy_version=settings.policy_version,
    )
    providers = ProviderRegistry(
        [
            MockAdapter(),
            OpenAIAdapter(
                api_key=_secret(settings.openai_api_key), base_url=settings.openai_base_url
            ),
            AnthropicAdapter(
                api_key=_secret(settings.anthropic_api_key),
                base_url=settings.anthropic_base_url,
            ),
            OllamaAdapter(base_url=settings.ollama_base_url),
        ]
    )
    telemetry = Telemetry(evidence)
    service = GatewayService(
        router=router,
        providers=providers,
        limiter=TokenBucketLimiter(
            rate_per_second=settings.request_rate_per_second,
            burst=settings.request_burst,
        ),
        circuits=circuits,
        cache=SemanticCache(
            ttl_seconds=settings.cache_ttl_seconds,
            similarity_threshold=settings.cache_similarity_threshold,
        ),
        artifacts=artifacts,
        releases=release_manager,
        telemetry=telemetry,
    )
    evaluations = EvaluationRunner(artifacts=artifacts, evidence=evidence, service=service)
    return Runtime(
        settings=settings,
        database=database,
        artifacts=artifacts,
        release_registry=release_registry,
        evidence=evidence,
        release_manager=release_manager,
        service=service,
        evaluations=evaluations,
    )


def _secret(value: object) -> str | None:
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if getter is not None else None
