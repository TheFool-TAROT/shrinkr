#!/usr/bin/env python3
"""压测脚本：异步并发打读路径（302 重定向），输出 QPS 与 P50/P95/P99。

用法：
    python bench/loadtest.py --base-url http://127.0.0.1:8000 --requests 20000 --concurrency 64

对比 wrk/ab 的好处：纯 Python + httpx，无需额外安装系统工具，方便在 CI 里跑回归。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def run(base_url: str, requests: int, concurrency: int, write_ratio: float, path: str | None = None) -> dict:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0, follow_redirects=False) as client:
        # 预热 + 准备样本：读路径打已经存在的短码，尽量让缓存命中
        seed = await client.post("/api/links", json={"url": "https://example.com/bench"})
        seed.raise_for_status()
        code = seed.json()["code"]
        for _ in range(3):
            await client.get(f"/{code}")

        read_urls = [path or f"/{code}"] * requests
        write_count = int(requests * write_ratio)
        if write_count:
            read_urls[:write_count] = ["__write__"] * write_count

        latencies: list[float] = []
        errors = 0
        sem = asyncio.Semaphore(concurrency)
        lat_lock = asyncio.Lock()

        async def one(path: str) -> None:
            nonlocal errors
            async with sem:
                started = time.perf_counter()
                try:
                    if path == "__write__":
                        res = await client.post("/api/links", json={"url": f"https://example.com/{time.time_ns()}"})
                    else:
                        res = await client.get(path)
                    if res.status_code >= 400:
                        errors += 1
                except Exception:
                    errors += 1
                elapsed = (time.perf_counter() - started) * 1000
                async with lat_lock:
                    latencies.append(elapsed)

        started = time.perf_counter()
        await asyncio.gather(*(one(u) for u in read_urls))
        wall = time.perf_counter() - started

    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = min(len(latencies) - 1, int(len(latencies) * p))
        return latencies[idx]

    return {
        "requests": len(latencies),
        "concurrency": concurrency,
        "wall_seconds": round(wall, 3),
        "qps": round(len(latencies) / wall, 1) if wall else 0.0,
        "errors": errors,
        "error_rate": round(errors / len(latencies), 6) if latencies else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50": round(pct(0.50), 3),
            "p90": round(pct(0.90), 3),
            "p95": round(pct(0.95), 3),
            "p99": round(pct(0.99), 3),
            "max": round(latencies[-1], 3) if latencies else 0.0,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="shrinkr load test")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--requests", type=int, default=20_000)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--write-ratio", type=float, default=0.0, help="写请求占比，默认纯读")
    ap.add_argument("--path", default=None, help="自定义压测路径，默认打 /{code} 的重定向")
    ap.add_argument("--json", action="store_true", help="只输出 JSON，便于 CI 采集")
    args = ap.parse_args()

    result = asyncio.run(run(args.base_url, args.requests, args.concurrency, args.write_ratio, args.path))
    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False))
    else:
        lat = result["latency_ms"]
        print(f"requests={result['requests']} concurrency={result['concurrency']} wall={result['wall_seconds']}s")
        print(f"QPS={result['qps']}  errors={result['errors']} ({result['error_rate'] * 100:.3f}%)")
        print(f"latency ms: mean={lat['mean']} p50={lat['p50']} p90={lat['p90']} p95={lat['p95']} p99={lat['p99']} max={lat['max']}")


if __name__ == "__main__":
    main()