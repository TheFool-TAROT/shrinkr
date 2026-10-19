"""核心业务逻辑：与 HTTP 框架解耦，可以单独测试，也方便将来换 FastAPI/Flask/Go。

一次创建请求的完整链路：
    URL 校验/归一化 -> 事务内 INSERT 拿自增 ID -> Base62 编码回写短码
    -> 写布隆过滤器 -> 写 LRU 缓存 -> 返回短链
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

from .base62 import code_for_id
from .bloom import BloomFilter
from .cache import LRUCache
from .clicks import ClickBuffer
from .config import Config
from .db import init_db, readonly, transaction
from .meters import Meters

CODE_RE = re.compile(r"^[0-9A-Za-z_-]{3,32}$")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
RESERVED_CODES = {"api", "admin", "static", "metrics", "healthz", "docs", "redoc", "openapi.json", "favicon.ico"}
SAFE_SCHEMES = {"http", "https"}


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------
class LinkError(Exception):
    status_code = 400
    detail = "bad request"


class InvalidURL(LinkError):
    status_code = 422
    detail = "url is not a valid absolute http(s) url"


class InvalidCode(LinkError):
    status_code = 422
    detail = "custom code must match [0-9A-Za-z_-]{3,32}"


class CodeTaken(LinkError):
    status_code = 409
    detail = "custom code already taken"


class InvalidTTL(LinkError):
    status_code = 422
    detail = "ttl_seconds out of range"


class NotFound(LinkError):
    status_code = 404
    detail = "short code not found"


class Gone(LinkError):
    status_code = 410
    detail = "short code expired"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def normalize_url(raw: str, max_length: int = 2048) -> str:
    if not isinstance(raw, str):
        raise InvalidURL()
    url = raw.strip()
    if not url or len(url) > max_length:
        raise InvalidURL()
    if not SCHEME_RE.match(url):  # 用户只贴 example.com/a?b=1 时补全 scheme
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in SAFE_SCHEMES or not parsed.netloc:
        raise InvalidURL()  # 同时挡掉 javascript:/data:/file: 这类被拿去做 XSS 的 scheme
    if " " in parsed.netloc or "." not in parsed.netloc:
        raise InvalidURL()
    return url


@dataclass
class ResolveResult:
    status: str  # ok | gone | missing
    url: str | None = None


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------
class LinkService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cache = LRUCache(cfg.cache_size)
        self.bloom = BloomFilter(cfg.bloom_capacity, cfg.bloom_error_rate)
        self.clicks = ClickBuffer(cfg.db_path, cfg.click_buffer_batch, cfg.click_buffer_interval)
        self.meters = Meters()

    # -- 启动/停止 --------------------------------------------------------
    def bootstrap(self) -> None:
        init_db(self.cfg)
        self.rebuild_bloom()
        self.clicks.start()

    def shutdown(self) -> None:
        self.clicks.stop()

    def rebuild_bloom(self) -> int:
        """启动时从 DB 重建索引；进程重启后内存结构必须能和持久层对齐。"""
        self.bloom.clear()
        now = int(time.time())
        with readonly(self.cfg) as conn:
            rows = conn.execute(
                "SELECT code FROM links WHERE expires_at IS NULL OR expires_at > ?", (now,)
            ).fetchall()
        for row in rows:
            self.bloom.add(row["code"])
        return len(rows)

    # -- 创建 -------------------------------------------------------------
    def create(
        self,
        url: str,
        custom_code: str | None = None,
        ttl_seconds: int | None = None,
        creator_ip: str | None = None,
    ) -> dict:
        clean_url = normalize_url(url, self.cfg.max_url_length)
        ttl = self.cfg.default_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl < 0 or ttl > self.cfg.max_ttl_seconds:
            raise InvalidTTL()
        if custom_code is not None and (
            not CODE_RE.match(custom_code) or custom_code.lower() in RESERVED_CODES
        ):
            raise InvalidCode()
        if self._is_self_referencing(clean_url):
            raise InvalidURL()

        now = int(time.time())
        expires_at = None if ttl == 0 else now + ttl

        try:
            with transaction(self.cfg) as conn:
                if custom_code:
                    cur = conn.execute(
                        "INSERT INTO links (code, url, created_at, expires_at, creator_ip) VALUES (?,?,?,?,?)",
                        (custom_code, clean_url, now, expires_at, creator_ip),
                    )
                    code = custom_code
                else:
                    cur = conn.execute(
                        "INSERT INTO links (code, url, created_at, expires_at, creator_ip) VALUES (?,?,?,?,?)",
                        (f"tmp_{uuid.uuid4().hex}", clean_url, now, expires_at, creator_ip),
                    )
                    code = code_for_id(cur.lastrowid)
                    conn.execute("UPDATE links SET code = ? WHERE id = ?", (code, cur.lastrowid))
        except Exception as exc:  # sqlite3.IntegrityError: UNIQUE constraint failed: links.code
            if "UNIQUE" in str(exc):
                raise CodeTaken() from exc
            raise

        self.bloom.add(code)
        self.cache.put(code, {"url": clean_url, "expires_at": expires_at})
        self.meters.inc("shrinkr_links_created_total")
        return self._public_view(code, clean_url, now, expires_at, 0)

    # -- 解析（热路径） ---------------------------------------------------
    def resolve(self, code: str) -> ResolveResult:
        if not CODE_RE.match(code or ""):
            self.meters.inc("shrinkr_not_found_total")
            return ResolveResult("missing")

        if not self.bloom.contains(code):
            # 布隆过滤器说没有 -> 一定没有，直接返回，不打 DB
            self.meters.inc("shrinkr_not_found_total")
            return ResolveResult("missing")

        entry = self.cache.get(code)
        if entry is None:
            with readonly(self.cfg) as conn:
                row = conn.execute(
                    "SELECT url, expires_at FROM links WHERE code = ?", (code,)
                ).fetchone()
            if row is None:  # 布隆过滤器假阳性，或链接已被删除
                self.meters.inc("shrinkr_not_found_total")
                return ResolveResult("missing")
            entry = {"url": row["url"], "expires_at": row["expires_at"]}
            self.cache.put(code, entry)

        if entry["expires_at"] is not None and entry["expires_at"] <= int(time.time()):
            self.cache.invalidate(code)
            self.meters.inc("shrinkr_gone_total")
            return ResolveResult("gone")
        return ResolveResult("ok", entry["url"])

    def record_click(self, code: str, referer: str | None, ua: str | None, ip: str | None) -> None:
        self.clicks.submit(code, (referer or "")[:512] or None, (ua or "")[:512] or None, ip)

    # -- 查询/删除 --------------------------------------------------------
    def get(self, code: str) -> dict:
        with readonly(self.cfg) as conn:
            row = conn.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
        if row is None:
            self.meters.inc("shrinkr_not_found_total")
            raise NotFound()
        if row["expires_at"] is not None and row["expires_at"] <= int(time.time()):
            raise Gone()
        return self._public_view(
            row["code"], row["url"], row["created_at"], row["expires_at"], row["clicks"]
        )

    def stats(self, code: str, days: int = 30) -> dict:
        self.clicks.flush()  # 统计接口容忍毫秒级延迟，先刷盘保证数字准确
        info = self.get(code)
        since = int(time.time()) - days * 86400
        with readonly(self.cfg) as conn:
            daily = conn.execute(
                """SELECT date(ts, 'unixepoch') AS day, count(*) AS clicks
                   FROM clicks WHERE code = ? AND ts >= ? GROUP BY day ORDER BY day ASC""",
                (code, since),
            ).fetchall()
            referers = conn.execute(
                """SELECT coalesce(nullif(referer, ''), '(direct)') AS referer, count(*) AS clicks
                   FROM clicks WHERE code = ? GROUP BY referer ORDER BY clicks DESC LIMIT 10""",
                (code,),
            ).fetchall()
            unique_ips = conn.execute(
                "SELECT count(DISTINCT ip) AS n FROM clicks WHERE code = ?", (code,)
            ).fetchone()["n"]
        info.update(
            {
                "total_clicks": info["clicks"],
                "unique_visitors": unique_ips,
                "clicks_by_day": [dict(r) for r in daily],
                "top_referers": [dict(r) for r in referers],
            }
        )
        return info

    def delete(self, code: str) -> bool:
        with transaction(self.cfg) as conn:
            row = conn.execute("SELECT id FROM links WHERE code = ?", (code,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM clicks WHERE code = ?", (code,))
            conn.execute("DELETE FROM links WHERE code = ?", (code,))
        self.cache.invalidate(code)
        # 布隆过滤器不支持删除：残留位只会造成少量假阳性，由 DB 回查兜底，交给周期性重建清理
        self.meters.inc("shrinkr_deleted_total")
        return True

    def cleanup_expired(self) -> int:
        now = int(time.time())
        with transaction(self.cfg) as conn:
            expired = conn.execute(
                "SELECT code FROM links WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            ).fetchall()
            if not expired:
                return 0
            codes = [r["code"] for r in expired]
            conn.executemany("DELETE FROM clicks WHERE code = ?", [(c,) for c in codes])
            conn.executemany("DELETE FROM links WHERE code = ?", [(c,) for c in codes])
        for code in codes:
            self.cache.invalidate(code)
        self.maybe_rebuild_bloom()
        return len(codes)

    def maybe_rebuild_bloom(self, threshold: float = 0.7) -> bool:
        """填充率过高说明假阳性变多（比如大量删除后残留），重建一次把噪声清掉。"""
        if self.bloom.fill_ratio() < threshold:
            return False
        self.rebuild_bloom()
        return True

    def overview(self, limit: int = 20) -> dict:
        now = int(time.time())
        with readonly(self.cfg) as conn:
            total = conn.execute("SELECT count(*) AS n FROM links").fetchone()["n"]
            active = conn.execute(
                "SELECT count(*) AS n FROM links WHERE expires_at IS NULL OR expires_at > ?", (now,)
            ).fetchone()["n"]
            clicks = conn.execute("SELECT coalesce(sum(clicks),0) AS n FROM links").fetchone()["n"]
            top = conn.execute(
                "SELECT code, url, clicks FROM links ORDER BY clicks DESC LIMIT ?", (limit,)
            ).fetchall()
        return {
            "total_links": total,
            "active_links": active,
            "expired_links": total - active,
            "total_clicks": clicks,
            "top_links": [dict(r) for r in top],
        }

    # -- 内部 -------------------------------------------------------------
    def _is_self_referencing(self, url: str) -> bool:
        host = urlparse(self.cfg.base_url).netloc.lower()
        return bool(host) and urlparse(url).netloc.lower() == host

    def _public_view(self, code, url, created_at, expires_at, clicks) -> dict:
        return {
            "code": code,
            "short_url": f"{self.cfg.base_url}/{code}",
            "url": url,
            "created_at": created_at,
            "expires_at": expires_at,
            "clicks": clicks,
        }