"""滑动窗口限流。

固定窗口在窗口交界处会放行 2 倍流量（临界突刺），滑动窗口用「时间戳队列」精确限流：
每个 key 保存窗口内每次请求的时间戳，判断时先弹出过期时间戳，再看队列长度。
单 key 内存占用 = limit 个 int，配合上限淘汰，不会被打爆。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int, max_keys: int = 100_000) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be > 0")
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """返回 (是否放行, 剩余额度, 需要等待的秒数)。"""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self.max_keys:
                    self._evict_stale(cutoff)
                bucket = self._hits[key] = deque()
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                retry_after = max(1, int(bucket[0] + self.window - now) + 1)
                return False, 0, retry_after
            bucket.append(now)
            return True, self.limit - len(bucket), 0

    def _evict_stale(self, cutoff: float) -> None:
        """只在表满时做一次全量清理，避免每次请求都遍历。"""
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def stats(self) -> dict:
        return {"tracked_keys": len(self._hits), "limit": self.limit, "window_seconds": self.window}