"""极简指标收集：线程安全的计数器 + 计时，导出 Prometheus 文本格式供 /metrics 抓取。"""

from __future__ import annotations

import threading
import time

_HELP: dict[str, str] = {
    "shrinkr_links_created_total": "已创建的短链总数",
    "shrinkr_redirects_total": "重定向成功次数",
    "shrinkr_not_found_total": "短码不存在次数",
    "shrinkr_gone_total": "短码已过期次数",
    "shrinkr_created_total": "写接口请求数",
    "shrinkr_rate_limited_total": "被限流拒绝的请求数",
    "shrinkr_deleted_total": "被删除的短链数",
    "shrinkr_errors_total": "5xx 次数",
}
_TYPE = "counter"


class Meters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {name: 0.0 for name in _HELP}
        self.gauges: dict[str, float] = {}
        self._started = time.time()

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = value

    def uptime(self) -> float:
        return time.time() - self._started

    def render(self, extra_gauges: dict[str, float] | None = None) -> str:
        with self._lock:
            counters = dict(self.counters)
            gauges = dict(self.gauges)
        if extra_gauges:
            gauges.update(extra_gauges)
        gauges["shrinkr_uptime_seconds"] = self.uptime()

        lines = []
        for name, value in sorted(counters.items()):
            if name in _HELP:
                lines.append(f"# HELP {name} {_HELP[name]}")
            lines.append(f"# TYPE {name} {_TYPE}")
            lines.append(f"{name} {value:g}")
        for name, value in sorted(gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value:g}")
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.counters, **self.gauges, "shrinkr_uptime_seconds": self.uptime()}