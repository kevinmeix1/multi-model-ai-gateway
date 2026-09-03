"""Tenant-scoped semantic response cache with policy-safe identity."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Protocol

from aegis_gateway.domain import GatewayRequest, GatewayResponse

_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)


class EmbeddingBackend(Protocol):
    def embed(self, text: str) -> dict[int, float]: ...


class HashingEmbedder:
    """Dependency-free deterministic embedding baseline for cache lookup."""

    def __init__(self, dimensions: int = 1024) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._dimensions = dimensions

    def embed(self, text: str) -> dict[int, float]:
        counts: dict[int, float] = {}
        for token in _TOKEN.findall(text.casefold()):
            digest = sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            counts[index] = counts.get(index, 0.0) + sign
        norm = math.sqrt(sum(value * value for value in counts.values()))
        if norm == 0:
            return {}
        return {index: value / norm for index, value in counts.items()}


@dataclass
class _Entry:
    scope: str
    fingerprint: str
    vector: dict[int, float]
    response: GatewayResponse
    expires_at: float


class SemanticCache:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        similarity_threshold: float,
        max_entries: int = 10_000,
        embedder: EmbeddingBackend | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0 or not 0 < similarity_threshold <= 1 or max_entries <= 0:
            raise ValueError("invalid cache configuration")
        self._ttl = ttl_seconds
        self._threshold = similarity_threshold
        self._max_entries = max_entries
        self._embedder = embedder or HashingEmbedder()
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(
        self, request: GatewayRequest, *, namespace: str = "default"
    ) -> GatewayResponse | None:
        scope, fingerprint, text = _identity(request, namespace=namespace)
        vector = self._embedder.embed(text)
        now = self._clock()
        async with self._lock:
            self._evict_expired(now)
            exact = self._entries.get(fingerprint)
            if exact is not None and exact.scope == scope:
                self._entries.move_to_end(fingerprint)
                return exact.response.model_copy(update={"cache_hit": True})
            best_key: str | None = None
            best_score = self._threshold
            for key, entry in self._entries.items():
                if entry.scope != scope:
                    continue
                score = _cosine(vector, entry.vector)
                if score >= best_score:
                    best_key = key
                    best_score = score
            if best_key is None:
                return None
            self._entries.move_to_end(best_key)
            return self._entries[best_key].response.model_copy(update={"cache_hit": True})

    async def put(
        self,
        request: GatewayRequest,
        response: GatewayResponse,
        *,
        namespace: str = "default",
    ) -> None:
        scope, fingerprint, text = _identity(request, namespace=namespace)
        entry = _Entry(
            scope=scope,
            fingerprint=fingerprint,
            vector=self._embedder.embed(text),
            response=response.model_copy(update={"cache_hit": False}),
            expires_at=self._clock() + self._ttl,
        )
        async with self._lock:
            self._entries[fingerprint] = entry
            self._entries.move_to_end(fingerprint)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def clear(self) -> int:
        async with self._lock:
            size = len(self._entries)
            self._entries.clear()
            return size

    async def size(self) -> int:
        async with self._lock:
            self._evict_expired(self._clock())
            return len(self._entries)

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


def _identity(request: GatewayRequest, *, namespace: str) -> tuple[str, str, str]:
    schema_hash = sha256(
        json.dumps(request.response_schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scope_data = {
        "namespace": namespace,
        "tenant": request.tenant_id,
        "classification": request.data_classification,
        "privacy": request.privacy_mode,
        "schema": schema_hash,
        "capabilities": sorted(request.required_capabilities),
        "allowed_regions": sorted(request.allowed_regions),
        "max_cost_usd": request.max_cost_usd,
        "max_latency_ms": request.max_latency_ms,
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
        "prompt": request.prompt.model_dump() if request.prompt else None,
    }
    text = "\n".join(f"{message.role}:{message.content}" for message in request.messages)
    scope = sha256(json.dumps(scope_data, sort_keys=True, default=str).encode()).hexdigest()
    fingerprint = sha256(f"{scope}\n{text}".encode()).hexdigest()
    return scope, fingerprint, text


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())
