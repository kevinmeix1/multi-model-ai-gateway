"""Reproducible local concurrency benchmark for gateway overhead and evidence metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from aegis_gateway.config import Settings
from aegis_gateway.domain import CacheMode, GatewayRequest, Message
from aegis_gateway.runtime import create_runtime


async def benchmark(requests: int, concurrency: int) -> dict[str, float | int | None]:
    with TemporaryDirectory(prefix="aegis-benchmark-") as directory:
        settings = Settings(
            database_path=Path(directory) / "aegis.db",
            request_rate_per_second=100_000,
            request_burst=max(requests, 100),
            log_level="WARNING",
        )
        runtime = create_runtime(settings)
        await runtime.initialize()
        semaphore = asyncio.Semaphore(concurrency)

        async def one(index: int) -> None:
            async with semaphore:
                await runtime.service.generate(
                    GatewayRequest(
                        tenant_id="benchmark",
                        request_id=f"benchmark-{index:06d}",
                        messages=[Message(role="user", content=f"benchmark request {index}")],
                        cache_mode=CacheMode.BYPASS,
                        shadow_enabled=False,
                    )
                )

        started = monotonic()
        await asyncio.gather(*(one(index) for index in range(requests)))
        elapsed = monotonic() - started
        summary = await runtime.evidence.summary()
        await runtime.aclose()
        return {
            **summary.model_dump(mode="json"),
            "wall_seconds": elapsed,
            "throughput_requests_per_second": requests / elapsed,
            "concurrency": concurrency,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--assert-slo", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(benchmark(args.requests, args.concurrency))
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.assert_slo:
        if result["success_rate"] != 1.0:
            raise SystemExit("benchmark success-rate gate failed")
        p99 = result["p99_latency_ms"]
        if not isinstance(p99, float | int) or p99 > 500:
            raise SystemExit("benchmark p99 gate failed")


if __name__ == "__main__":
    main()
