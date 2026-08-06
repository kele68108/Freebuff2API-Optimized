import json
import unittest

from freebuff2api.metrics import Metrics
from freebuff2api.codebuff import CodebuffAccountPool
from freebuff2api.config import Settings
from freebuff2api.app import _metric_path, _rss_bytes


def _settings() -> Settings:
    return Settings(
        codebuff_token="token-a,token-b",
        local_api_key=None,
    )


class MetricsRenderTests(unittest.TestCase):
    def test_render_outputs_valid_prometheus_text(self) -> None:
        metrics = Metrics()
        metrics.observe("GET", "/v1/models", 200, 0.012)
        metrics.observe("GET", "/v1/models", 200, 0.03)
        metrics.observe("POST", "/v1/chat/completions", 502, 2.5)

        body = metrics.render(
            accounts_total=2,
            accounts_healthy=1,
            accounts_busy=0,
            rss_bytes=123456,
        )

        self.assertIn("# TYPE freebuff2api_http_requests_total counter", body)
        self.assertIn(
            'freebuff2api_http_requests_total{method="GET",path="/v1/models",status="200"} 2',
            body,
        )
        self.assertIn(
            'freebuff2api_http_requests_total{method="POST",path="/v1/chat/completions",status="502"} 1',
            body,
        )
        self.assertIn("# TYPE freebuff2api_http_request_duration_seconds histogram", body)
        self.assertIn('le="Inf"', body)
        self.assertIn("freebuff2api_accounts_total 2", body)
        self.assertIn("freebuff2api_accounts_healthy 1", body)
        self.assertIn("process_resident_memory_bytes 123456", body)
        self.assertIn("freebuff2api_uptime_seconds", body)

    def test_histogram_buckets_are_cumulative(self) -> None:
        metrics = Metrics()
        metrics.observe("GET", "/x", 200, 0.012)  # lands in le=0.025 bucket
        body = metrics.render(
            accounts_total=1,
            accounts_healthy=1,
            accounts_busy=0,
            rss_bytes=1,
        )
        self.assertIn(
            'freebuff2api_http_request_duration_seconds_bucket{method="GET",path="/x",le="0.025"} 1',
            body,
        )
        self.assertIn(
            'freebuff2api_http_request_duration_seconds_bucket{method="GET",path="/x",le="0.005"} 0',
            body,
        )
        # sum equals observed duration
        self.assertIn(
            'freebuff2api_http_request_duration_seconds_sum{method="GET",path="/x"} 0.012000',
            body,
        )


class MetricPathTests(unittest.TestCase):
    def test_known_routes_unchanged(self) -> None:
        self.assertEqual(_metric_path("/v1/chat/completions"), "/v1/chat/completions")
        self.assertEqual(_metric_path("/metrics"), "/metrics")
        self.assertEqual(_metric_path("/readyz"), "/readyz")

    def test_unknown_routes_collapse_to_other(self) -> None:
        # 404s / scanner probes must not create unbounded label cardinality
        self.assertEqual(_metric_path("/unknown"), "{other}")
        self.assertEqual(_metric_path("/foo/bar/baz"), "{other}")
        self.assertEqual(_metric_path("/wp-admin/x"), "{other}")

    def test_dynamic_segments_on_unknown_base_collapse_to_other(self) -> None:
        # A route this app does not serve (even with an id-looking segment)
        # must collapse to {other}, never to a per-segment label.
        self.assertEqual(
            _metric_path("/api/v1/agent-runs/0123456789abcdef/steps"),
            "{other}",
        )


class PoolHealthTests(unittest.TestCase):
    def test_healthy_account_count_reflects_rate_limit_quarantine(self) -> None:
        pool = CodebuffAccountPool(_settings())
        self.assertEqual(pool.healthy_account_count(), 2)

        pool.mark_rate_limited(0, "deepseek/deepseek-v4-flash", None)
        self.assertEqual(pool.healthy_account_count(), 1)

        pool.mark_rate_limited(1, "deepseek/deepseek-v4-flash", None)
        self.assertEqual(pool.healthy_account_count(), 0)

    def test_healthy_account_count_ignores_transient_busy(self) -> None:
        pool = CodebuffAccountPool(_settings())
        pool._accounts[0].busy = True
        # busy is transient — still healthy
        self.assertEqual(pool.healthy_account_count(), 2)
        self.assertEqual(pool.busy_account_count(), 1)


class RssBytesTests(unittest.TestCase):
    def test_rss_bytes_positive(self) -> None:
        self.assertGreater(_rss_bytes(), 0)


if __name__ == "__main__":
    unittest.main()
