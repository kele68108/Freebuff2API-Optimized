from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

# Minimal Prometheus text-format registry (exposition format 0.0.4).
# Deliberately dependency-free: adding prometheus_client would churn uv.lock
# and the pinned baseline. Implements one counter (requests by method/path/
# status) and one histogram (latency with cumulative buckets), plus gauges
# fed by the caller at render time.

_HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, float("inf"),
)


def _escape(value: Any) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self._started_at = time.time()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._buckets: dict[tuple[str, str], list[int]] = defaultdict(
            lambda: [0] * len(_HISTOGRAM_BUCKETS)
        )
        self._sums: dict[tuple[str, str], float] = defaultdict(float)
        self._counts: dict[tuple[str, str], int] = defaultdict(int)

    def observe(self, method: str, path: str, status: int, duration: float) -> None:
        key = (method, path, str(status))
        self._requests[key] += 1
        hist = (method, path)
        self._counts[hist] += 1
        self._sums[hist] += duration
        for index, upper in enumerate(_HISTOGRAM_BUCKETS):
            if duration <= upper:
                self._buckets[hist][index] += 1

    def render(
        self,
        *,
        accounts_total: int,
        accounts_healthy: int,
        accounts_busy: int,
        rss_bytes: int,
    ) -> str:
        lines: list[str] = []
        lines.append("# HELP freebuff2api_http_requests_total Total HTTP requests processed.")
        lines.append("# TYPE freebuff2api_http_requests_total counter")
        for (method, path, status), count in sorted(self._requests.items()):
            lines.append(
                f'freebuff2api_http_requests_total{{method="{_escape(method)}",'
                f'path="{_escape(path)}",status="{status}"}} {count}'
            )

        lines.append("# HELP freebuff2api_http_request_duration_seconds HTTP request latency.")
        lines.append("# TYPE freebuff2api_http_request_duration_seconds histogram")
        for (method, path) in sorted(self._counts):
            bucket_counts = self._buckets[(method, path)]
            for index, upper in enumerate(_HISTOGRAM_BUCKETS):
                le = "Inf" if upper == float("inf") else str(upper)
                lines.append(
                    f'freebuff2api_http_request_duration_seconds_bucket{{method="{_escape(method)}",'
                    f'path="{_escape(path)}",le="{le}"}} {bucket_counts[index]}'
                )
            lines.append(
                f'freebuff2api_http_request_duration_seconds_sum{{method="{_escape(method)}",'
                f'path="{_escape(path)}"}} {self._sums[(method, path)]:.6f}'
            )
            lines.append(
                f'freebuff2api_http_request_duration_seconds_count{{method="{_escape(method)}",'
                f'path="{_escape(path)}"}} {self._counts[(method, path)]}'
            )

        lines.append("# HELP freebuff2api_accounts_total Total configured upstream accounts.")
        lines.append("# TYPE freebuff2api_accounts_total gauge")
        lines.append(f"freebuff2api_accounts_total {accounts_total}")
        lines.append("# HELP freebuff2api_accounts_healthy Healthy (non-blocked) upstream accounts.")
        lines.append("# TYPE freebuff2api_accounts_healthy gauge")
        lines.append(f"freebuff2api_accounts_healthy {accounts_healthy}")
        lines.append("# HELP freebuff2api_accounts_busy Accounts currently busy serving a request.")
        lines.append("# TYPE freebuff2api_accounts_busy gauge")
        lines.append(f"freebuff2api_accounts_busy {accounts_busy}")
        lines.append("# HELP process_resident_memory_bytes Resident memory size in bytes.")
        lines.append("# TYPE process_resident_memory_bytes gauge")
        lines.append(f"process_resident_memory_bytes {rss_bytes}")
        lines.append("# HELP freebuff2api_uptime_seconds Seconds since process start.")
        lines.append("# TYPE freebuff2api_uptime_seconds gauge")
        lines.append(f"freebuff2api_uptime_seconds {int(time.time() - self._started_at)}")
        return "\n".join(lines) + "\n"
