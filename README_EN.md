<div align="center">

# shrinkr

**Self-hosted URL shortener that survives read-heavy traffic — without Redis**

Bloom filter hot-path filtering · LRU cache · sliding-window rate limiting · batched click writes · single-file SQLite, zero external services

[![CI](https://github.com/TheFool-TAROT/shrinkr/actions/workflows/ci.yml/badge.svg)](https://github.com/TheFool-TAROT/shrinkr/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

English | [简体中文](README.md)

</div>

---

## The problem

Real traffic on a shortener is **read-heavy**, and a large share of those reads are for codes that **do not exist** (scanners, typos, expired links). A naive implementation hits the database for every one of them.

shrinkr's rule: **anything that can be answered in memory never touches disk.**

| Layer | Structure | Role | Measured |
|---|---|---|---|
| 1 | Bloom filter | "definitely absent" → 404, zero DB access | 260k ops/s, 200k entries in 351 KB |
| 2 | LRU cache | hot codes → instant 302 | 1.2M ops/s |
| 3 | SQLite (WAL) | fallback for correctness | cold path |

On the write side, a synchronous `UPDATE` per click is textbook write amplification. Clicks go into an in-memory queue and a background thread merges them into batched transactions — **writes drop from O(requests) to O(batches)**.

## Measured results

2 vCPU box, Python 3.14, single `uvicorn` worker, `ab` running on the same machine (so numbers are conservative).

| Concurrency | QPS | P50 | P95 | P99 | Failures |
|---|---|---|---|---|---|
| 50 | 1061 | 46 ms | 60 ms | 70 ms | 0 |
| **100** | **1122** | **89 ms** | **111 ms** | **131 ms** | **0** |
| 200 | 970 | 210 ms | 247 ms | 259 ms | 0 |

Before → after moving click writes off the request path: **QPS 773 → 1122 (+45%)**, P50 129 ms → 89 ms, P99 172 ms → 131 ms.

Integrity check after 60,000 redirects: `/metrics` redirect counter and the `clicks` table both read exactly 60,000; cache hit rate 1.0; estimated bloom false-positive rate 0.

Micro-benchmarks (`python bench/micro.py`): `bloom.contains` 260k ops/s (3.84 µs) · `base62` 786k ops/s (1.27 µs) · `lru.get` 1.21M ops/s (0.83 µs) · `ratelimit.check` 1.05M ops/s (0.95 µs).

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000     # UI at /, OpenAPI at /docs

docker compose up -d                           # or one-command deploy
```

```bash
curl -X POST http://127.0.0.1:8000/api/links -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/very/long/path?x=1","custom_code":"promo2025"}'
curl -sI http://127.0.0.1:8000/promo2025 | grep -i location
curl -s http://127.0.0.1:8000/api/links/promo2025/stats
```

## Features

- Base62 bijective encoding of auto-increment IDs (collision-free by construction) + custom aliases with reserved-word protection
- Per-link TTL with lazy expiry and a background cleanup thread
- Click analytics: totals, unique visitors, daily series, top referrers
- Sliding-window rate limiting per IP with `Retry-After`, `TRUST_PROXY` gate for `X-Forwarded-For`
- URL validation rejecting `javascript:` / `data:` / `file:` schemes and self-referencing links
- Prometheus-format `/metrics`, `/healthz`, Bearer-token admin endpoints
- 46 tests covering data-structure invariants, concurrency behavior and API edge cases; GitHub Actions runs lint → tests → smoke test → load test → Docker build

## Design decisions

- **No Redis** — the target is a self-hosted box where extra services cost more in ops risk than they save. `cache.py` / `bloom.py` are the swap points when scaling out.
- **SQLite + WAL** — reads concurrent, writes serialized: a good fit for read-heavy workloads, and it pushes complexity back into code instead of into operations.
- **Bloom filter without deletion** — stale bits only cause false positives (an extra DB lookup that returns 404), never false negatives. Rebuild is triggered past 70% fill ratio.
- **Batched click writes may lose one batch** on `kill -9` — a deliberate trade for analytics data; switch to Redis/Kafka when exactly-once matters.

## License

[MIT](LICENSE)