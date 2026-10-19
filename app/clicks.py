"""点击事件批量写入器。

每次 302 都同步写一条 clicks 是典型的写放大：热点链接会把整个 DB 的写锁占满。
这里把事件先塞进内存队列，由后台线程按「攒够 N 条」或「每 T 秒」两种条件刷盘，
一个事务里批量 INSERT + 一次 UPDATE 累加计数，写入次数从 O(请求数) 降到 O(批次)。

代价是进程被 kill -9 时最多丢一个批次（约 0.5s），对点击统计这种非关键数据可以接受；
真要求不丢就换 Redis/Kafka，README 里写了演进路线。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque

from .db import connect

INSERT_CLICK = "INSERT INTO clicks (code, ts, referer, ua, ip) VALUES (?, ?, ?, ?, ?)"
BUMP_COUNTER = "UPDATE links SET clicks = clicks + ?, last_click_at = ? WHERE code = ?"


class ClickBuffer:
    def __init__(self, db_path: str, batch_size: int = 200, flush_interval: float = 0.5) -> None:
        self.db_path = db_path
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.05, flush_interval)
        self._queue: deque[tuple] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.flushed_events = 0
        self.flushes = 0
        self.dropped = 0

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="click-flusher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self.flush()

    # -- 生产/消费 --------------------------------------------------------
    def submit(self, code: str, referer: str | None, ua: str | None, ip: str | None,
               ts: int | None = None) -> None:
        """热路径上只做一次 list append，绝不碰磁盘、绝不等 IO。

        队列攒够一个批次时顺手叫醒刷盘线程，让它立刻合并写，而不是死等 flush_interval。
        """
        with self._lock:
            self._queue.append((code, int(time.time()) if ts is None else ts, referer, ua, ip))
            ready = len(self._queue) >= self.batch_size
        if ready:
            self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_interval)  # 定时刷 或 被批次阈值叫醒
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.flush()
            except sqlite3.Error:  # pragma: no cover - 磁盘故障时不让线程死掉
                continue

    def flush(self) -> int:
        with self._lock:
            if not self._queue:
                return 0
            batch = list(self._queue)
            self._queue.clear()

        rows: dict[str, list[tuple]] = {}
        for code, ts, referer, ua, ip in batch:
            rows.setdefault(code, []).append((code, ts, referer, ua, ip))

        conn = connect(_ConfigProxy(self.db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            for code, events in rows.items():
                conn.executemany(INSERT_CLICK, events)
                last_ts = max(e[1] for e in events)
                conn.execute(BUMP_COUNTER, (len(events), last_ts, code))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            with self._lock:
                self.dropped += len(batch)
            raise
        finally:
            conn.close()

        with self._lock:
            self.flushed_events += len(batch)
            self.flushes += 1
        return len(batch)

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def stats(self) -> dict:
        return {
            "pending": self.pending(),
            "flushed_events": self.flushed_events,
            "flushes": self.flushes,
            "dropped": self.dropped,
            "batch_size": self.batch_size,
            "flush_interval": self.flush_interval,
        }


class _ConfigProxy:
    """只用到 db_path，避免为了连接数据库把整个 Config 传进来。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path