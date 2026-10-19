"""布隆过滤器：挡住「不存在的短码」这类穿透查询。

短链服务的读流量里很大一部分是扫描器/输错的码，全部落到 SQLite 上会被打满。
布隆过滤器 O(1) 内存 + 单次位运算判断，**没有假阴性**：说没有就一定没有。
代价是删除不支持、有假阳性，所以命中之后仍要回查 DB 兜底。

参数推导：位数组长度 m = -n·ln(p)/(ln2)^2，哈希个数 k = (m/n)·ln2。
默认 n=200k、p=0.1% -> m≈2.87Mbit（约 350KB），k=10。
"""

from __future__ import annotations

import hashlib
import math
import threading


class BloomFilter:
    def __init__(self, capacity: int = 200_000, error_rate: float = 0.001) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if not 0 < error_rate < 1:
            raise ValueError("error_rate must be in (0, 1)")

        self.capacity = capacity
        self.error_rate = error_rate
        self.num_bits = max(64, int(-(capacity * math.log(error_rate)) / (math.log(2) ** 2)))
        self.num_hashes = max(1, round((self.num_bits / capacity) * math.log(2)))
        self._bits = bytearray((self.num_bits + 7) // 8)
        self._items = 0
        self._lock = threading.Lock()

    # -- 内部 -------------------------------------------------------------
    def _positions(self, item: str):
        """Kirsch-Mitzenmacher：用两个独立哈希线性组合出 k 个位置，避免算 k 次摘要。"""
        digest = hashlib.sha256(item.encode("utf-8")).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:16], "big") | 1  # 必须为奇数，保证与 m 互质
        for i in range(self.num_hashes):
            yield (h1 + i * h2) % self.num_bits

    # -- 公共接口 ---------------------------------------------------------
    def add(self, item: str) -> None:
        with self._lock:
            for pos in self._positions(item):
                self._bits[pos >> 3] |= 1 << (pos & 7)
            self._items += 1

    def contains(self, item: str) -> bool:
        return all(self._bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(item))

    __contains__ = contains

    def clear(self) -> None:
        with self._lock:
            self._bits = bytearray(len(self._bits))
            self._items = 0

    def fill_ratio(self) -> float:
        bits_set = sum(bin(b).count("1") for b in self._bits)
        return bits_set / self.num_bits

    def estimated_false_positive_rate(self) -> float:
        """按当前真实填充率反推假阳性概率，比理论值更诚实。"""
        return self.fill_ratio() ** self.num_hashes

    @property
    def items(self) -> int:
        return self._items

    def stats(self) -> dict:
        return {
            "capacity": self.capacity,
            "target_error_rate": self.error_rate,
            "items": self._items,
            "num_bits": self.num_bits,
            "num_hashes": self.num_hashes,
            "size_bytes": len(self._bits),
            "fill_ratio": round(self.fill_ratio(), 6),
            "estimated_fp_rate": round(self.estimated_false_positive_rate(), 8),
        }