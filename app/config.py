"""运行期配置：全部来自环境变量，保证容器里零改代码。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DAY = 24 * 3600


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    db_path: str = "data/shrinkr.db"
    base_url: str = "http://127.0.0.1:8000"
    admin_token: str = ""
    default_ttl_seconds: int = 0  # 0 表示永不过期
    max_ttl_seconds: int = 365 * DAY
    max_url_length: int = 2048

    cache_size: int = 20_000
    bloom_capacity: int = 200_000
    bloom_error_rate: float = 0.001

    rate_limit: int = 60  # 每个窗口允许的写请求数
    rate_window_seconds: int = 60
    trust_proxy: bool = False

    cleanup_interval_seconds: int = 60
    click_buffer_batch: int = 200
    click_buffer_interval: float = 0.5

    def __post_init__(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            db_path=os.environ.get("SHRINKR_DB", "data/shrinkr.db"),
            base_url=os.environ.get("SHRINKR_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            admin_token=os.environ.get("SHRINKR_ADMIN_TOKEN", ""),
            default_ttl_seconds=_env_int("SHRINKR_DEFAULT_TTL", 0),
            max_ttl_seconds=_env_int("SHRINKR_MAX_TTL", 365 * DAY),
            max_url_length=_env_int("SHRINKR_MAX_URL_LENGTH", 2048),
            cache_size=_env_int("SHRINKR_CACHE_SIZE", 20_000),
            bloom_capacity=_env_int("SHRINKR_BLOOM_CAPACITY", 200_000),
            bloom_error_rate=_env_float("SHRINKR_BLOOM_ERROR_RATE", 0.001),
            rate_limit=_env_int("SHRINKR_RATE_LIMIT", 60),
            rate_window_seconds=_env_int("SHRINKR_RATE_WINDOW", 60),
            trust_proxy=_env_bool("SHRINKR_TRUST_PROXY", False),
            cleanup_interval_seconds=_env_int("SHRINKR_CLEANUP_INTERVAL", 60),
            click_buffer_batch=_env_int("SHRINKR_CLICK_BATCH", 200),
            click_buffer_interval=_env_float("SHRINKR_CLICK_INTERVAL", 0.5),
        )