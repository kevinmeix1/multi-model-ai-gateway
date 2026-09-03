"""Concurrency-safe per-tenant token-bucket rate limiting."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from aegis_gateway.errors import RateLimitExceededError


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if rate_per_second <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str, tokens: float = 1) -> float:
        """Consume capacity and return remaining tokens, or fail without sleeping."""

        if tokens <= 0 or tokens > self._burst:
            raise ValueError("tokens must be in (0, burst]")
        async with self._lock:
            now = self._clock()
            bucket = self._buckets.setdefault(key, _Bucket(self._burst, now))
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now
            if bucket.tokens < tokens:
                retry_after = (tokens - bucket.tokens) / self._rate
                raise RateLimitExceededError(
                    f"tenant rate limit exceeded; retry after {retry_after:.3f}s"
                )
            bucket.tokens -= tokens
            return bucket.tokens
