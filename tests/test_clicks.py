"""点击批量写入器的行为测试：攒批、聚合更新、stop 时兜底刷盘。"""

from __future__ import annotations

import time

from app.clicks import ClickBuffer
from app.config import Config
from app.db import init_db, readonly


def test_batch_flush_aggregates_counters(tmp_path):
    cfg = Config(db_path=str(tmp_path / "b.db"))
    init_db(cfg)
    buf = ClickBuffer(cfg.db_path, batch_size=1000, flush_interval=60)
    buf.submit("abc", "https://ref", "curl/8", "127.0.0.1", ts=1000)
    buf.submit("abc", None, "curl/8", "127.0.0.1", ts=1001)
    buf.submit("abc", None, "curl/8", "127.0.0.2", ts=1002)
    buf.submit("xyz", None, None, "127.0.0.1", ts=1003)

    assert buf.pending() == 4
    assert buf.flush() == 4
    assert buf.pending() == 0
    assert buf.flush() == 0

    with readonly(cfg) as conn:
        rows = conn.execute("SELECT code, count(*) n FROM clicks GROUP BY code ORDER BY code").fetchall()
    assert {r["code"]: r["n"] for r in rows} == {"abc": 3, "xyz": 1}


def test_batch_threshold_wakes_flusher_thread(tmp_path):
    """队列攒满一个批次时立刻叫醒刷盘线程，而不是干等 flush_interval。"""
    cfg = Config(db_path=str(tmp_path / "b2.db"))
    init_db(cfg)
    buf = ClickBuffer(cfg.db_path, batch_size=3, flush_interval=30)
    buf.start()
    try:
        for i in range(3):
            buf.submit("abc", None, None, None, ts=1000 + i)
        deadline = time.time() + 2
        while buf.pending() and time.time() < deadline:
            time.sleep(0.01)
        assert buf.pending() == 0
        assert buf.stats()["flushes"] >= 1
    finally:
        buf.stop()


def test_stop_drains_remaining_events(tmp_path):
    cfg = Config(db_path=str(tmp_path / "b3.db"))
    init_db(cfg)
    buf = ClickBuffer(cfg.db_path, batch_size=10_000, flush_interval=30)
    buf.start()
    buf.submit("abc", None, None, None, ts=1000)
    assert buf.pending() == 1     # 既没攒满批次，也没到 flush_interval
    buf.stop()                    # 关机路径：先停线程再兜底 flush，不丢事件
    assert buf.pending() == 0
    with readonly(cfg) as conn:
        n = conn.execute("SELECT count(*) n FROM clicks").fetchone()["n"]
    assert n == 1