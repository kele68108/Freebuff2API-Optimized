import unittest
from unittest import mock

from freebuff2api.app import _rate_limit_delay_ms
from freebuff2api.codebuff import CodebuffError


class RateLimitDelayTests(unittest.TestCase):
    def test_prefers_retry_after_ms(self) -> None:
        error = CodebuffError("429", 502, is_rate_limit=True, retry_after_ms=1200)
        with mock.patch("random.uniform", return_value=0.0):
            self.assertEqual(_rate_limit_delay_ms(error, jitter_ms=250), 1200.0)

    def test_falls_back_to_reset_at_when_no_retry_after(self) -> None:
        # reset_at 3 seconds in the future
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat().replace(
            "+00:00", "Z"
        )
        error = CodebuffError("429", 502, is_rate_limit=True, reset_at=future)
        with mock.patch("random.uniform", return_value=0.0):
            delay = _rate_limit_delay_ms(error, jitter_ms=250)
        self.assertGreaterEqual(delay, 2500.0)
        self.assertLessEqual(delay, 4000.0)

    def test_uses_default_when_no_hints(self) -> None:
        error = CodebuffError("429", 502, is_rate_limit=True)
        with mock.patch("random.uniform", return_value=0.0):
            self.assertEqual(_rate_limit_delay_ms(error, jitter_ms=250), 500.0)

    def test_caps_at_five_seconds(self) -> None:
        error = CodebuffError("429", 502, is_rate_limit=True, retry_after_ms=30000)
        with mock.patch("random.uniform", return_value=0.0):
            self.assertEqual(_rate_limit_delay_ms(error, jitter_ms=250), 5000.0)

    def test_adds_jitter_within_range(self) -> None:
        error = CodebuffError("429", 502, is_rate_limit=True, retry_after_ms=1000)
        # force max jitter
        with mock.patch("random.uniform", return_value=249.0):
            self.assertEqual(_rate_limit_delay_ms(error, jitter_ms=250), 1249.0)

    def test_jitter_disabled_when_zero(self) -> None:
        error = CodebuffError("429", 502, is_rate_limit=True, retry_after_ms=1000)
        with mock.patch("random.uniform", return_value=999.0):
            # jitter_ms=0 -> random.uniform must NOT be called / not added
            self.assertEqual(_rate_limit_delay_ms(error, jitter_ms=0), 1000.0)

    def test_malformed_reset_at_falls_through_to_default(self) -> None:
        error = CodebuffError("429", 502, is_rate_limit=True, reset_at="not-a-date")
        with mock.patch("random.uniform", return_value=0.0):
            self.assertEqual(_rate_limit_delay_ms(error, jitter_ms=250), 500.0)


if __name__ == "__main__":
    unittest.main()
