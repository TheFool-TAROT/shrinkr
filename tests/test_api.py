from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import transaction
from app.main import create_app
from tests.conftest import make_link


def test_create_returns_short_url(client):
    body = make_link(client)
    assert body["code"]
    assert body["short_url"] == f"http://testserver/{body['code']}"
    assert body["url"] == "https://example.com/a/b?c=1"
    assert body["expires_at"] is None
    assert body["clicks"] == 0


def test_redirect_counts_click(client):
    code = make_link(client)["code"]
    res = client.get(f"/{code}", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "https://example.com/a/b?c=1"

    stats = client.get(f"/api/links/{code}/stats").json()
    assert stats["total_clicks"] == 1
    assert stats["unique_visitors"] == 1
    assert stats["top_referers"][0]["clicks"] == 1


def test_query_string_is_forwarded(client):
    code = make_link(client)["code"]
    res = client.get(f"/{code}?utm_source=x", follow_redirects=False)
    assert res.headers["location"] == "https://example.com/a/b?c=1&utm_source=x"

    plain = make_link(client, url="https://example.com/plain")["code"]
    res2 = client.get(f"/{plain}?a=1", follow_redirects=False)
    assert res2.headers["location"] == "https://example.com/plain?a=1"


def test_head_redirect_does_not_count_as_click(client):
    """爬虫/链接预览用 HEAD 探测，不能污染点击数。"""
    code = make_link(client)["code"]
    res = client.head(f"/{code}", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "https://example.com/a/b?c=1"
    assert client.get(f"/api/links/{code}/stats").json()["total_clicks"] == 0


def test_scheme_is_inferred(client):
    body = make_link(client, url="example.com/path")
    assert body["url"] == "https://example.com/path"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "", "http://", "a" * 3000])
def test_invalid_urls_rejected(client, bad):
    res = client.post("/api/links", json={"url": bad})
    assert res.status_code == 422


def test_self_referencing_url_rejected(client):
    res = client.post("/api/links", json={"url": "http://testserver/abc"})
    assert res.status_code == 422


def test_custom_code_and_conflict(client):
    first = make_link(client, custom_code="promo2025")
    assert first["code"] == "promo2025"
    dup = client.post("/api/links", json={"url": "https://example.org", "custom_code": "promo2025"})
    assert dup.status_code == 409


@pytest.mark.parametrize("bad", ["ab", "a" * 33, "bad code", "上号", "api"])
def test_invalid_custom_codes(client, bad):
    res = client.post("/api/links", json={"url": "https://example.org", "custom_code": bad})
    assert res.status_code == 422


def test_unknown_code_404_and_never_touches_db(client, service):
    before = service.bloom.items
    assert client.get("/zzzzzz", follow_redirects=False).status_code == 404
    assert service.bloom.items == before
    assert client.get("/api/links/zzzzzz").status_code == 404
    assert service.meters.snapshot()["shrinkr_not_found_total"] >= 2


def test_expired_link_returns_410(client, cfg, service):
    code = make_link(client, ttl_seconds=60)["code"]
    with transaction(cfg) as conn:
        conn.execute("UPDATE links SET expires_at = 1 WHERE code = ?", (code,))
    service.cache.invalidate(code)
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410
    assert client.get(f"/api/links/{code}").status_code == 410
    assert client.get(f"/api/links/{code}/stats").status_code == 410


def test_ttl_limits(client):
    assert client.post("/api/links", json={"url": "https://a.com", "ttl_seconds": -1}).status_code == 422
    assert client.post("/api/links", json={"url": "https://a.com", "ttl_seconds": 10**9}).status_code == 422


def test_cleanup_expired_removes_rows(client, cfg, service):
    code = make_link(client, ttl_seconds=60)["code"]
    with transaction(cfg) as conn:
        conn.execute("UPDATE links SET expires_at = 1 WHERE code = ?", (code,))
    assert service.cleanup_expired() == 1
    assert service.cleanup_expired() == 0
    assert client.get(f"/api/links/{code}").status_code == 404


def test_delete_requires_admin_token(client):
    code = make_link(client)["code"]
    assert client.delete(f"/api/links/{code}").status_code == 401
    assert client.delete(f"/api/links/{code}", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.delete(f"/api/links/{code}", headers={"Authorization": "Bearer s3cret"}).status_code == 204
    assert client.get(f"/api/links/{code}").status_code == 404
    assert client.delete(f"/api/links/{code}", headers={"Authorization": "Bearer s3cret"}).status_code == 404


def test_metrics_and_health(client):
    make_link(client)
    health = client.get("/healthz").json()
    assert health["status"] == "ok"

    text = client.get("/metrics").text
    for expected in ("shrinkr_links_created_total", "shrinkr_cache_hit_rate", "shrinkr_bloom_fill_ratio"):
        assert expected in text


def test_index_page_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "shrinkr" in res.text


def test_rate_limit_returns_429(tmp_path):
    cfg = Config(db_path=str(tmp_path / "rl.db"), base_url="http://testserver", rate_limit=2, rate_window_seconds=60)
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.post("/api/links", json={"url": "https://a.com"}).status_code == 201
        assert c.post("/api/links", json={"url": "https://b.com"}).status_code == 201
        blocked = c.post("/api/links", json={"url": "https://c.com"})
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1


def test_forwarded_header_ignored_without_trust_proxy(client):
    for i in range(3):
        client.post("/api/links", json={"url": f"https://a{i}.com"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
    # trust_proxy=False 时所有请求都算同一个 client，限流额度被共享
    assert client.app.state.service.meters.snapshot()["shrinkr_created_total"] >= 3