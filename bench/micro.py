#!/usr/bin/env python3
"""微基准：把三个核心数据结构单独拉出来测吞吐，用来证明「热路径不碰磁盘」这句结论。

用法：python bench/micro.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.base62 import code_for_id, decode  # noqa: E402
from app.bloom import BloomFilter  # noqa: E402
from app.cache import LRUCache  # noqa: E402
from app.ratelimit import SlidingWindowLimiter  # noqa: E402


def bench(name: str, fn, iterations: int = 500_000) -> dict:
    fn()  # 预热
    started = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - started
    ops = iterations / elapsed
    print(f"{name:<34} {ops:>12,.0f} ops/s   ({elapsed / iterations * 1e6:.2f} µs/op)")
    return {"name": name, "ops_per_sec": round(ops), "us_per_op": round(elapsed / iterations * 1e6, 3)}


def main() -> None:
    print(f"{'benchmark':<34} {'throughput':>12}")
    print("-" * 66)

    bloom = BloomFilter(capacity=200_000, error_rate=0.001)
    for i in range(200_000):
        bloom.add(f"code{i}")

    counter = {"n": 0}
    def next_code() -> str:
        counter["n"] += 1
        return code_for_id(counter["n"])

    cache = LRUCache(maxsize=20_000)
    for i in range(20_000):
        cache.put(f"code{i}", {"url": f"https://example.com/{i}", "expires_at": None})
    n = {"i": 0}
    def cache_get() -> None:
        n["i"] = (n["i"] + 1) % 20_000
        cache.get(f"code{n['i']}")

    limiter = SlidingWindowLimiter(limit=1_000_000, window_seconds=60)
    def limit_check() -> None:
        limit_check.counter += 1
        limiter.check(f"10.0.0.{limit_check.counter % 255}")
    limit_check.counter = 0

    results = [
        bench("bloom.contains (20万条已加载)", lambda: bloom.contains("code123456")),
        bench("base62.encode + decode", lambda: decode(next_code())),
        bench("lru.get (2万条容量满)", cache_get),
        bench("ratelimit.check (滑动窗口)", limit_check),
    ]

    print("-" * 66)
    print(f"bloom: bits={bloom.num_bits:,} hashes={bloom.num_hashes} size={len(bloom._bits) / 1024:.0f} KB "
          f"fill={bloom.fill_ratio():.2%} est_fp={bloom.estimated_false_positive_rate():.2e}")
    print(f"cache hit rate={cache.stats()['hit_rate']:.4f}  evictions={cache.stats()['evictions']}")
    print(f"result json: {results}")


if __name__ == "__main__":
    main()