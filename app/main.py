"""HTTP 层：FastAPI 薄封装，业务逻辑全在 LinkService 里。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Config
from .db import readonly
from .ratelimit import SlidingWindowLimiter
from .service import Gone, LinkError, LinkService, NotFound

__version__ = "1.0.0"
log = logging.getLogger("shrinkr")
STATIC_DIR = Path(__file__).parent / "static"


class CreateLinkRequest(BaseModel):
    url: str = Field(..., description="原始长链接，缺省 scheme 时按 https 补全")
    custom_code: str | None = Field(default=None, description="自定义短码，[0-9A-Za-z_-]{3,32}")
    ttl_seconds: int | None = Field(default=None, ge=0, description="过期秒数，0/缺省表示永不过期")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    service = LinkService(cfg)
    limiter = SlidingWindowLimiter(cfg.rate_limit, cfg.rate_window_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.bootstrap()
        log.info("shrinkr %s ready, db=%s", __version__, cfg.db_path)
        try:
            yield
        finally:
            service.shutdown()

    app = FastAPI(title="shrinkr", version=__version__, description="自托管短链服务", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.service = service
    app.state.limiter = limiter

    # ---------------------------------------------------------------- 异常
    @app.exception_handler(LinkError)
    async def _link_error(_: Request, exc: LinkError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # ------------------------------------------------------------ 限流依赖
    def rate_limit(request: Request) -> None:
        key = client_ip(request, cfg)
        allowed, remaining, retry_after = limiter.check(key)
        if not allowed:
            service.meters.inc("shrinkr_rate_limited_total")
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded: {cfg.rate_limit}/{cfg.rate_window_seconds}s",
                headers={"Retry-After": str(retry_after)},
            )
        request.state.rate_remaining = remaining

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        if not cfg.admin_token:
            raise HTTPException(status_code=503, detail="admin api disabled: SHRINKR_ADMIN_TOKEN is empty")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != cfg.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin token")

    # ---------------------------------------------------------------- 路由
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return HTMLResponse("<h1>shrinkr</h1><p>UI 未打包，请使用 /api/links 接口。</p>")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.post("/api/links", status_code=201, dependencies=[Depends(rate_limit)])
    def create_link(payload: CreateLinkRequest, request: Request, response: Response) -> dict:
        service.meters.inc("shrinkr_created_total")
        link = service.create(
            payload.url,
            custom_code=payload.custom_code,
            ttl_seconds=payload.ttl_seconds,
            creator_ip=client_ip(request, cfg),
        )
        if hasattr(request.state, "rate_remaining"):
            response.headers["X-RateLimit-Remaining"] = str(request.state.rate_remaining)
        return link

    @app.get("/api/links/{code}")
    def get_link(code: str) -> dict:
        return service.get(code)

    @app.get("/api/links/{code}/stats")
    def link_stats(code: str, days: int = 30) -> dict:
        return service.stats(code, days=days)

    @app.delete("/api/links/{code}", status_code=204, dependencies=[Depends(require_admin)])
    def delete_link(code: str) -> Response:
        if not service.delete(code):
            raise NotFound()
        return Response(status_code=204)

    @app.get("/api/overview", dependencies=[Depends(require_admin)])
    def overview(limit: int = 20) -> dict:
        return service.overview(limit=limit)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        try:
            with readonly(cfg) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # pragma: no cover - 仅在存储故障时进入
            service.meters.inc("shrinkr_errors_total")
            raise HTTPException(status_code=503, detail=f"storage unavailable: {exc}") from exc
        return {"status": "ok", "version": __version__, "uptime_seconds": round(service.meters.uptime(), 1)}

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    def metrics() -> PlainTextResponse:
        extra = {
            "shrinkr_cache_hit_rate": service.cache.stats()["hit_rate"],
            "shrinkr_cache_size": service.cache.stats()["size"],
            "shrinkr_bloom_items": service.bloom.items,
            "shrinkr_bloom_fill_ratio": round(service.bloom.fill_ratio(), 6),
            "shrinkr_bloom_estimated_fp_rate": round(service.bloom.estimated_false_positive_rate(), 8),
            "shrinkr_click_queue_pending": service.clicks.pending(),
        }
        return PlainTextResponse(service.meters.render(extra), media_type="text/plain; version=0.0.4")

    # ---------------------------------------------------- 通配短码跳转（必须放最后）
    # 路由按声明顺序匹配，/healthz、/metrics 这些固定路径必须先注册，
    # 否则会被 /{code} 吃掉；短码侧则用 RESERVED_CODES 拒绝保留字。
    @app.api_route("/{code}", methods=["GET", "HEAD"], response_model=None)
    def redirect(code: str, request: Request) -> Response:
        result = service.resolve(code)
        if result.status == "missing":
            raise NotFound()
        if result.status == "gone":
            raise Gone()
        target = result.url or ""
        if request.url.query:
            sep = "&" if "?" in target else "?"
            target = f"{target}{sep}{request.url.query}"
        service.meters.inc("shrinkr_redirects_total")
        # HEAD 是爬虫/预览卡片的探测请求，不计入点击；GET 才进内存队列（一次 append），
        # 由后台线程合并落盘，请求线程不等 IO
        if request.method == "GET":
            service.record_click(
                code,
                request.headers.get("referer"),
                request.headers.get("user-agent"),
                client_ip(request, cfg),
            )
        return RedirectResponse(
            url=target,
            status_code=302,
            headers={"Cache-Control": "no-cache, private", "Referrer-Policy": "unsafe-url"},
        )

    return app


def client_ip(request: Request, cfg: Config) -> str:
    """TRUST_PROXY=1 时才相信 X-Forwarded-For，否则任何人都能伪造 header 绕过限流。"""
    if cfg.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


app = create_app()