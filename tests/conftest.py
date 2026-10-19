from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        db_path=str(tmp_path / "shrinkr.db"),
        base_url="http://testserver",
        admin_token="s3cret",
        rate_limit=10_000,
        rate_window_seconds=60,
        click_buffer_batch=10,
        click_buffer_interval=0.05,
    )


@pytest.fixture
def client(cfg: Config):
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def service(cfg: Config, client):
    return client.app.state.service


def make_link(client, url: str = "https://example.com/a/b?c=1", **kw) -> dict:
    res = client.post("/api/links", json={"url": url, **kw})
    assert res.status_code == 201, res.text
    return res.json()