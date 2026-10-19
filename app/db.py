"""SQLite 访问层：一次请求一条连接，WAL 模式，写操作走 BEGIN IMMEDIATE。

选 SQLite 而不是 Postgres/Redis 的理由：
1. 单文件部署，`docker run` 就能跑，自托管的人不需要额外运维成本；
2. WAL 模式下读可以并发、写串行，短链这种「读远多于写」的场景刚好对口；
3. 把复杂度留在代码里（缓存、布隆、批量写）而不是留在运维里。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,
    url           TEXT    NOT NULL,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER,
    clicks        INTEGER NOT NULL DEFAULT 0,
    last_click_at INTEGER,
    creator_ip    TEXT
);

CREATE INDEX IF NOT EXISTS idx_links_expires ON links(expires_at);

CREATE TABLE IF NOT EXISTS clicks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    code    TEXT    NOT NULL,
    ts      INTEGER NOT NULL,
    referer TEXT,
    ua      TEXT,
    ip      TEXT
);

CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code, ts);
"""


def connect(cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path, timeout=10.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(cfg: Config) -> None:
    conn = connect(cfg)
    try:
        if cfg.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(cfg: Config, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """显式事务：immediate=True 立刻拿写锁，避免 SQLite 的升级写锁死锁(ERR: database is locked)。"""
    conn = connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def readonly(cfg: Config) -> Iterator[sqlite3.Connection]:
    conn = connect(cfg)
    try:
        yield conn
    finally:
        conn.close()