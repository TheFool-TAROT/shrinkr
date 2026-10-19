from __future__ import annotations

import random

from app.bloom import BloomFilter


def test_no_false_negatives():
    """布隆过滤器的核心不变量：加过的元素一定判为存在。"""
    bf = BloomFilter(capacity=1000, error_rate=0.01)
    items = [f"code-{i}" for i in range(1000)]
    for item in items:
        bf.add(item)
    assert all(item in bf for item in items)


def test_false_positive_rate_within_budget():
    bf = BloomFilter(capacity=10_000, error_rate=0.01)
    for i in range(10_000):
        bf.add(f"k{i}")

    probes = [f"probe-{i}" for i in range(20_000)]
    fp = sum(1 for p in probes if p in bf) / len(probes)
    assert fp < 0.02  # 目标 1%，留一倍余量


def test_parameter_derivation():
    bf = BloomFilter(capacity=200_000, error_rate=0.001)
    # m = -n·ln(p)/(ln2)^2 ≈ 2.87e6 bit, k ≈ 10
    assert 2.5e6 < bf.num_bits < 3.2e6
    assert 8 <= bf.num_hashes <= 12
    assert bf.stats()["size_bytes"] < 512 * 1024  # 20 万链接的索引不到 512KB


def test_clear_resets_state():
    bf = BloomFilter(capacity=100, error_rate=0.01)
    bf.add("x")
    assert "x" in bf
    bf.clear()
    assert "x" not in bf
    assert bf.items == 0


def test_random_keys_no_false_negative():
    rng = random.Random(42)
    bf = BloomFilter(capacity=5000, error_rate=0.005)
    keys = {f"{rng.randrange(10**9):x}" for _ in range(5000)}
    for k in keys:
        bf.add(k)
    assert all(k in bf for k in keys)