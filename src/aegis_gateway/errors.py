"""Domain errors with explicit retry and HTTP semantics."""

from __future__ import annotations


class AegisError(Exception):
    """Base class for expected gateway failures."""

    code = "gateway_error"
    status_code = 500
    retryable = False


class NoEligibleRouteError(AegisError):
    code = "no_eligible_route"
    status_code = 422


class RateLimitExceededError(AegisError):
    code = "rate_limit_exceeded"
    status_code = 429
    retryable = True


class BudgetExceededError(AegisError):
    code = "budget_exceeded"
    status_code = 422


class ProviderError(AegisError):
    """Normalized upstream failure."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int = 502,
        retryable: bool = True,
        code: str = "provider_error",
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.code = code


class ProviderAuthError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(
            message,
            provider=provider,
            status_code=502,
            retryable=False,
            code="provider_auth_error",
        )


class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(
            message,
            provider=provider,
            status_code=503,
            retryable=True,
            code="provider_rate_limited",
        )


class ProviderTimeoutError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(
            message,
            provider=provider,
            status_code=504,
            retryable=True,
            code="provider_timeout",
        )


class CircuitOpenError(AegisError):
    code = "circuit_open"
    status_code = 503
    retryable = True


class RegistryConflictError(AegisError):
    code = "registry_conflict"
    status_code = 409


class ArtifactNotFoundError(AegisError):
    code = "artifact_not_found"
    status_code = 404


class EvaluationGateError(AegisError):
    code = "evaluation_gate_failed"
    status_code = 409


class SchemaViolationError(AegisError):
    code = "schema_violation"
    status_code = 502
    retryable = True


class StreamInterruptedError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(
            message,
            provider=provider,
            status_code=502,
            retryable=False,
            code="stream_interrupted",
        )
