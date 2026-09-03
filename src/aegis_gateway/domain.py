"""Provider-independent contracts used by every gateway subsystem."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class Capability(StrEnum):
    TEXT = "text"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOLS = "tools"
    VISION = "vision"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class PrivacyMode(StrEnum):
    STANDARD = "standard"
    ZERO_RETENTION = "zero_retention"
    LOCAL_ONLY = "local_only"


class CacheMode(StrEnum):
    DEFAULT = "default"
    BYPASS = "bypass"
    REFRESH = "refresh"


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=1_000_000)
    name: str | None = Field(default=None, max_length=128)


class ArtifactRef(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    version: str | None = Field(default=None, max_length=128)


class GatewayRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=128)
    user_id: str | None = Field(default=None, max_length=256)
    messages: list[Message] = Field(min_length=1, max_length=256)
    prompt: ArtifactRef | None = None
    prompt_variables: dict[str, str] = Field(default_factory=dict)
    required_capabilities: set[Capability] = Field(default_factory=lambda: {Capability.TEXT})
    data_classification: DataClassification = DataClassification.INTERNAL
    privacy_mode: PrivacyMode = PrivacyMode.STANDARD
    allowed_regions: set[str] = Field(default_factory=set)
    max_cost_usd: float = Field(default=0.05, ge=0, le=1000)
    max_latency_ms: int = Field(default=10_000, ge=50, le=600_000)
    max_output_tokens: int = Field(default=512, ge=1, le=131_072)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False
    response_schema: dict[str, Any] | None = None
    schema_name: str = Field(default="gateway_response", pattern=r"^[a-zA-Z0-9_-]+$")
    cache_mode: CacheMode = CacheMode.DEFAULT
    shadow_enabled: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def infer_capabilities(self) -> GatewayRequest:
        self.required_capabilities.add(Capability.TEXT)
        if self.stream:
            self.required_capabilities.add(Capability.STREAMING)
        if self.response_schema is not None:
            self.required_capabilities.add(Capability.STRUCTURED_OUTPUT)
        return self


class ModelRoute(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    capabilities: set[Capability]
    regions: set[str] = Field(min_length=1)
    context_window: int = Field(gt=0)
    input_cost_per_million: float = Field(ge=0)
    output_cost_per_million: float = Field(ge=0)
    expected_p95_latency_ms: int = Field(gt=0)
    quality_score: float = Field(ge=0, le=1)
    privacy_modes: set[PrivacyMode]
    enabled: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)


class RejectedRoute(StrictModel):
    route_id: str
    reasons: list[str]


class RoutingCandidate(StrictModel):
    route_id: str
    predicted_cost_usd: float
    predicted_latency_ms: int
    utility: float
    routing_regret: float = 0


class RoutingDecision(StrictModel):
    request_id: str
    selected_route_id: str
    candidates: list[RoutingCandidate]
    rejected: list[RejectedRoute]
    policy_version: str
    canary: bool = False
    release_id: str | None = None


class ProviderResult(StrictModel):
    provider_request_id: str | None = None
    text: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    finish_reason: str = "stop"
    ttft_ms: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    raw_model: str


class ProviderStreamEvent(StrictModel):
    type: Literal["start", "delta", "usage", "done", "error"]
    delta: str = ""
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    provider_request_id: str | None = None


class GatewayUsage(StrictModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float


class GatewayResponse(StrictModel):
    request_id: str
    route_id: str
    provider: str
    model: str
    text: str
    parsed: Any | None = None
    schema_valid: bool | None = None
    usage: GatewayUsage
    ttft_ms: float
    latency_ms: float
    cache_hit: bool = False
    fallback_count: int = 0
    routing_regret: float = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GatewayStreamEvent(StrictModel):
    type: Literal["start", "delta", "done", "error"]
    request_id: str
    route_id: str
    provider: str
    model: str
    delta: str = ""
    response: GatewayResponse | None = None
    error_code: str | None = None


class ArtifactKind(StrEnum):
    PROMPT = "prompt"
    DATASET = "dataset"
    MODEL_CATALOG = "model_catalog"
    POLICY = "policy"


class Artifact(StrictModel):
    kind: ArtifactKind
    name: str
    version: str
    content: dict[str, Any]
    content_hash: str
    created_at: datetime


class ReleaseState(StrEnum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class Release(StrictModel):
    id: str
    name: str
    baseline_route_id: str
    candidate_route_id: str
    canary_percent: int = Field(default=5, ge=0, le=100)
    shadow_percent: int = Field(default=0, ge=0, le=100)
    state: ReleaseState = ReleaseState.DRAFT
    max_error_rate: float = Field(default=0.05, ge=0, le=1)
    max_p99_latency_ms: float = Field(default=10_000, gt=0)
    min_schema_compliance: float = Field(default=0.99, ge=0, le=1)
    min_quality_score: float = Field(default=0.8, ge=0, le=1)
    min_canary_samples: int = Field(default=20, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RequestMetric(StrictModel):
    request_id: str
    tenant_id: str
    route_id: str
    provider: str
    success: bool
    schema_valid: bool | None
    cache_hit: bool
    canary: bool
    release_id: str | None = None
    shadow: bool = False
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ttft_ms: float
    latency_ms: float
    routing_regret: float
    fallback_count: int
    error_code: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvaluationCase(StrictModel):
    id: str
    messages: list[Message]
    expected_text: str | None = None
    expected_contains: list[str] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    tags: set[str] = Field(default_factory=set)


class EvaluationCaseResult(StrictModel):
    case_id: str
    route_id: str
    passed: bool
    quality_score: float
    schema_valid: bool | None
    latency_ms: float
    cost_usd: float
    error: str | None = None


class EvaluationRun(StrictModel):
    id: str
    dataset_name: str
    dataset_version: str
    route_id: str
    results: list[EvaluationCaseResult]
    pass_rate: float
    mean_quality: float
    schema_compliance: float | None
    p99_latency_ms: float
    total_cost_usd: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MetricsSnapshot(StrictModel):
    requests: int
    successful_requests: int
    success_rate: float
    schema_compliance: float | None
    cache_hit_rate: float
    failover_success_rate: float | None
    mean_cost_per_success_usd: float | None
    mean_ttft_ms: float | None
    p99_latency_ms: float | None
    mean_routing_regret: float | None
