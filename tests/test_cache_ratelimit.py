from __future__ import annotations

from app.cache import LRUCache
from app.ratelimit import SlidingWindowLimiter


def test_lru_evicts_least_recently_used():
    cache = LRUCache(maxsize=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")          # a 变成最近使用
    cache.put("c", 3)       # 淘汰 b
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3
    assert cache.stats()["evictions"] == 1


def test_cache_hit_rate():
    cache = LRUCache(maxsize=10)
    cache.put("a", 1)
    cache.get("a")
    cache.get("miss")
    stats = cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["hit_rate"] == 0.5


def test_limiter_allows_then_blocks():
    rl = SlidingWindowLimiter(limit=3, window_seconds=60)
    for i in range(3):
        allowed, remaining, _ = rl.check("1.2.3.4", now=1000.0)
        assert allowed
        assert remaining == 2 - i
    allowed, remaining, retry_after = rl.check("1.2.3.4", now=1000.0)
    assert not allowed and remaining == 0 and retry_after >= 1


def test_limiter_window_slides():
    rl = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert rl.check("ip", now=0.0)[0]
    assert rl.check("ip", now=1.0)[0]
    assert not rl.check("ip", now=2.0)[0]
    assert rl.check("ip", now=10.5)[0]   # 第一次请求已滑出窗口


def test_limiter_is_per_key():
    rl = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert rl.check("a", now=0.0)[0]
    assert not rl.check("a", now=0.0)[0]
    assert rl.check("b", now=0.0)[0]


def test_limiter_evicts_stale_keys_when_full():
    """表满时才做一次清理：老 key 的最后一个时间戳滑出窗口即被淘汰。"""
    rl = SlidingWindowLimiter(limit=1, window_seconds=10, max_keys=5)
    for i in range(10):
        rl.check(f"ip-{i}", now=float(i * 20))
    stats = rl.stats()
    assert stats["tracked_keys"] <= 6